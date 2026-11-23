"""LLM 增强层：把结构化事实翻译成人能直接用的诊断说明。

这一层的定位
----------
**它不负责发现根因。** 根因候选由 diagnose.py 的确定性规则产出，LLM 只做三件事：
  1. 在给定候选里排序，说明为什么这个更可能
  2. 把证据组织成一段人能快速读懂的话
  3. 给出下一步最该执行的那一两条命令（从已给的 verify 列表里挑）

幻觉约束的三道闸
-------------
1. **输入闭集**：prompt 里只给 payload 事实 + 规则候选，并明确要求不得引入
   候选之外的根因。
2. **输出结构化**：要求返回 JSON，字段固定。自由文本无法校验，JSON 可以。
3. **输出校验**：解析失败、引用了不存在的 rule_id、缺必需字段，一律判定为
   失败并降级到规则层输出。**宁可没有 AI 解读，也不要错的解读。**

降级是第一公民
-----------
告警通道的可用性高于一切。LLM 超时、限流、网关异常、返回垃圾——全部走降级，
且降级原因必须出现在最终输出里，让人知道「这段话是规则给的还是模型给的」。
绝不允许静默降级：那会让人误以为模型认可了规则结论。
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# 默认模型名可用环境变量 AI_MODEL 覆盖。
# 注意：模型名会随供应商下线旧版本而失效（本项目实测时
# `claude-sonnet-4-20250514` 返回 400 "model has been deprecated"）。
# 这种失败会正常降级到规则层，不会中断告警通道，
# 但要记得定期确认默认模型名仍然可用。
DEFAULT_MODEL = os.environ.get("AI_MODEL", "claude-opus-4-1-20250805")
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_TOKENS = 900


def _build_ssl_context() -> ssl.SSLContext:
    """构造 TLS 上下文。

    为什么需要这一步：macOS 上 Framework 版的 Python 不会自动读系统证书库，
    直接请求会报 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`
    （本项目首次真实调用就撞上了，当时正确地降级成了规则层结论）。

    **绝不能为了跑通而关闭校验**——告警内容会被明文读到中间人手里。
    这里按优先级找一个可用的 CA 包；都找不到时仍用默认上下文，
    让请求失败并降级，而不是降级安全性。

    优先级：
      1. 环境变量 AI_CA_BUNDLE / SSL_CERT_FILE（显式指定，便于内网自签 CA）
      2. certifi 自带的 CA 包（装了就用）
      3. 系统 CA 文件
    """
    for env_name in ("AI_CA_BUNDLE", "SSL_CERT_FILE"):
        path = os.environ.get(env_name)
        if path and os.path.exists(path):
            try:
                return ssl.create_default_context(cafile=path)
            except (ssl.SSLError, OSError):
                continue

    try:
        import certifi  # 可选依赖

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(path):
            try:
                return ssl.create_default_context(cafile=path)
            except (ssl.SSLError, OSError):
                continue

    return ssl.create_default_context()


SYSTEM_PROMPT = """你是协助值班运维工程师的告警分析助手。

你会收到：
1. 一个已经聚合好的告警事件的客观事实（来自 Alertmanager webhook，未经推断）
2. 规则引擎给出的候选根因列表，每条带 rule_id、证据、反向判别条件、验证命令

严格约束：
- 只能从给定的候选根因里选择和排序，**不得引入候选之外的根因**
- 不得编造事实。事实里没有的数值、主机名、时间、日志内容一律不许出现
- next_commands 只能从候选的 verify 列表里挑选，不得自己发明命令
- 如果证据不足以区分几个候选，必须说明「需要先执行哪条命令才能区分」，而不是硬选一个
- 用中文，面向正在处理故障的人，直接说结论和下一步，不要客套

只输出一个 JSON 对象，不要代码块包裹，不要任何额外文字：
{
  "headline": "一句话结论，不超过 60 字",
  "ranked_rule_ids": ["按可能性从高到低排列的 rule_id"],
  "reasoning": "为什么这样排序，依据哪条证据。不超过 200 字",
  "next_commands": ["最该先执行的 1-3 条命令，从 verify 里选"],
  "uncertainty": "当前无法确定的是什么，需要什么信息才能确定。不超过 100 字"
}"""


@dataclass
class LLMResult:
    """LLM 调用结果。ok=False 时上层必须降级，并把 degraded_reason 展示出来。"""

    ok: bool
    headline: str = ""
    ranked_rule_ids: list[str] = field(default_factory=list)
    reasoning: str = ""
    next_commands: list[str] = field(default_factory=list)
    uncertainty: str = ""
    model: str = ""
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)
    degraded_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "headline": self.headline,
            "ranked_rule_ids": self.ranked_rule_ids,
            "reasoning": self.reasoning,
            "next_commands": self.next_commands,
            "uncertainty": self.uncertainty,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "degraded_reason": self.degraded_reason,
        }


class LLMClient:
    """Anthropic Messages API 的极简客户端。

    只依赖标准库，没有第三方包——目标机是 1.6G 内存的 ECS，
    而且告警链路上少一个依赖就少一个故障点。

    支持 ANTHROPIC_CUSTOM_HEADERS（每行 `Key: Value`），
    用于需要额外鉴权头的网关环境。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        extra_headers: str | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = (
            base_url if base_url is not None else os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        ).rstrip("/")
        self.model = model
        self.timeout = timeout
        self._raw_extra = (
            extra_headers if extra_headers is not None else os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        )
        self._ssl_context = _build_ssl_context()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
        }
        for line in self._raw_extra.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                if key:
                    headers[key] = value.strip()
        return headers

    def complete(self, prompt: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple[str, dict]:
        """发一次请求，返回 (文本, usage)。失败抛异常，由上层转成降级。

        ⚠️ 真实环境差异：有些网关无视请求体里的 `stream` 参数，一律返回
        `text/event-stream`。本项目实测时就是这样——按 JSON 解析会报
        `Expecting value: line 1 column 1`，然后被降级逻辑拦下（降级是对的，
        但功能等于没用上）。所以这里按 content-type 分支处理两种响应。
        """
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout, context=self._ssl_context
        ) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            raw = response.read().decode("utf-8", "ignore")

        if "event-stream" in content_type:
            return _parse_sse(raw)
        data = json.loads(raw)
        chunks = [
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(chunks).strip(), data.get("usage", {}) or {}


def _parse_sse(raw: str) -> tuple[str, dict]:
    """解析 SSE 流，把增量文本拼起来。

    只需要 content_block_delta 里的 text_delta；其他事件类型（message_start、
    ping、message_stop）忽略。这样不必实现完整的流式状态机——
    本工具用法是「一次性拿完整结果」，不是边生成边展示。
    """
    text_parts: list[str] = []
    usage: dict = {}

    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                text_parts.append(delta["text"])
        elif event_type == "message_start":
            message = event.get("message") or {}
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                usage.update(message["usage"])
        elif event_type == "message_delta":
            if isinstance(event.get("usage"), dict):
                usage.update(event["usage"])

    text = "".join(text_parts).strip()
    if not text:
        raise ValueError("SSE 响应里没有文本内容")
    return text, usage


def build_prompt(facts: dict, findings: list[dict]) -> str:
    """构造 user 消息。事实与候选分开放，边界清楚。"""
    payload = {
        "incident_facts": facts,
        "candidate_root_causes": [
            {
                "rule_id": f["rule_id"],
                "hypothesis": f["hypothesis"],
                "rule_confidence": f["confidence"],
                "evidence": f["evidence"],
                "counter_evidence": f["counter_evidence"],
                "verify": f["verify"],
            }
            for f in findings
        ],
        "allowed_rule_ids": [f["rule_id"] for f in findings],
    }
    return (
        "请分析下面这个告警事件。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n只输出 JSON 对象。"
    )


def _extract_json(text: str) -> dict:
    """从模型输出里取 JSON。

    模型有时会套 ```json 代码块或在前后加说明，所以做有限度的容错：
    截取第一个 { 到最后一个 } 之间的内容。再失败就判定为失败——
    不做更激进的修补，解析不出来就该降级。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [ln for ln in stripped.splitlines() if not ln.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中找不到 JSON 对象")
    return json.loads(stripped[start : end + 1])


def _validate(data: dict, allowed_rule_ids: set[str]) -> tuple[bool, str]:
    """校验模型输出。任何一项不合格都判失败。"""
    if not isinstance(data, dict):
        return False, "输出不是 JSON 对象"

    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        return False, "缺少 headline"

    ranked = data.get("ranked_rule_ids")
    if not isinstance(ranked, list) or not ranked:
        return False, "缺少 ranked_rule_ids"
    if not all(isinstance(x, str) for x in ranked):
        return False, "ranked_rule_ids 含非字符串元素"

    # 关键闸门：引用了不存在的 rule_id，说明模型在编造候选
    unknown = [x for x in ranked if x not in allowed_rule_ids]
    if unknown:
        return False, f"引用了候选之外的 rule_id: {unknown}"

    for key in ("reasoning", "uncertainty"):
        if key in data and not isinstance(data[key], str):
            return False, f"{key} 类型错误"

    commands = data.get("next_commands", [])
    if not isinstance(commands, list) or not all(isinstance(x, str) for x in commands):
        return False, "next_commands 类型错误"

    return True, ""


def enhance(
    facts: dict,
    findings: list[dict],
    *,
    client: LLMClient | None = None,
    retries: int = 1,
) -> LLMResult:
    """对单个事件做 LLM 增强。任何异常都转成 ok=False，绝不向上抛。"""
    allowed = {f["rule_id"] for f in findings}
    if not allowed:
        return LLMResult(ok=False, degraded_reason="没有候选根因，跳过 LLM")

    client = client or LLMClient()
    if not client.available:
        return LLMResult(ok=False, degraded_reason="未配置 ANTHROPIC_API_KEY，仅输出规则层结论")

    prompt = build_prompt(facts, findings)
    last_error = ""

    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            text, usage = client.complete(prompt)
            latency = int((time.monotonic() - started) * 1000)
            data = _extract_json(text)
            valid, reason = _validate(data, allowed)
            if not valid:
                last_error = f"输出校验失败：{reason}"
                continue
            return LLMResult(
                ok=True,
                headline=str(data["headline"]).strip(),
                ranked_rule_ids=list(data["ranked_rule_ids"]),
                reasoning=str(data.get("reasoning", "")).strip(),
                next_commands=list(data.get("next_commands", [])),
                uncertainty=str(data.get("uncertainty", "")).strip(),
                model=client.model,
                latency_ms=latency,
                usage=usage,
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:200].decode("utf-8", "ignore")
            except Exception:
                pass
            last_error = f"HTTP {exc.code}: {detail}"
        except urllib.error.URLError as exc:
            last_error = f"网络错误: {exc.reason}"
        except TimeoutError:
            last_error = f"超时（{client.timeout}s）"
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"解析失败: {exc}"
        except Exception as exc:  # pragma: no cover - 兜底，不能让告警链路挂掉
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))

    return LLMResult(ok=False, degraded_reason=f"LLM 不可用，已降级到规则层（{last_error}）")
