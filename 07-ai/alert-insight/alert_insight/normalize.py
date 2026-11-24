"""告警归一化：把 Alertmanager webhook payload 拆成扁平的单条告警记录。

设计原则
--------
webhook payload（schema version 4）是「一组」告警，字段分三层：
  groupLabels / commonLabels / commonAnnotations  —— 组级
  alerts[].labels / annotations                   —— 单条级

归因需要的是单条级事实，所以先拍平。拍平时保留 groupKey，
因为 groupKey 记录了这组告警最终落在哪条 route 上（排查「为什么没发到某个群」时唯一可靠的线索）。

为什么不信任 payload 里的时间
--------------------------
startsAt 是 Prometheus 判定 firing 的时间，不是故障发生时间（受 `for` 参数影响）。
本模块只做格式转换，不做任何时间推断，推断留给 aggregate 层并显式标注。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

# Alertmanager firing 时 endsAt 的零值
_ZERO_TIME = "0001-01-01T00:00:00Z"

SUPPORTED_PAYLOAD_VERSIONS = {"4"}


class PayloadError(ValueError):
    """payload 结构不符合预期。宁可显式报错，也不要静默吞掉半截数据。"""


@dataclass(frozen=True)
class Alert:
    """一条归一化后的告警。

    fingerprint 是 Alertmanager 对 label set 的哈希，是判断「是否同一条告警」
    的唯一可靠依据。注意：label 改一个字 fingerprint 就变，所以告警规则里
    不能放会变动的 label（带时间戳、带当前值），否则去重彻底失效。
    """

    fingerprint: str
    alertname: str
    status: str  # firing | resolved
    severity: str
    instance: str
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    starts_at: _dt.datetime | None = None
    ends_at: _dt.datetime | None = None
    generator_url: str = ""
    group_key: str = ""
    receiver: str = ""

    @property
    def summary(self) -> str:
        return (
            self.annotations.get("summary")
            or self.annotations.get("description")
            or self.alertname
        )

    @property
    def job(self) -> str:
        return self.labels.get("job", "")

    def label_subset(self, keys: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        """取指定 label 的子集，用于分组键。缺失的 label 不参与。"""
        return tuple((k, self.labels[k]) for k in keys if k in self.labels)


def parse_time(raw: str | None) -> _dt.datetime | None:
    """解析 RFC3339 时间。firing 的零值 endsAt 返回 None。

    Alertmanager 的小数秒位数不固定（.18Z / .180000Z / 无小数），
    Python 3.11+ 的 fromisoformat 能吃 Z，但小数位必须是 3 或 6 位，
    所以这里统一补齐，不依赖第三方库。
    """
    if not raw or raw == _ZERO_TIME:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # 补齐小数秒到 6 位
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        if digits:
            digits = (digits + "000000")[:6]
            text = f"{head}.{digits}{rest}"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - 防御性
        raise PayloadError(f"无法解析时间字段: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _as_str_map(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PayloadError(f"{field_name} 应为对象，实际为 {type(value).__name__}")
    return {str(k): "" if v is None else str(v) for k, v in value.items()}


def normalize(payload: dict[str, Any], *, strict_version: bool = False) -> list[Alert]:
    """把一个 webhook payload 转成 Alert 列表。

    strict_version=True 时遇到未知 schema 版本直接报错；默认只是放行，
    因为真实环境里 Alertmanager 升级往往早于接收器升级，宁可降级处理
    也不要让告警通道中断。
    """
    if not isinstance(payload, dict):
        raise PayloadError("payload 必须是 JSON 对象")

    version = str(payload.get("version", ""))
    if strict_version and version not in SUPPORTED_PAYLOAD_VERSIONS:
        raise PayloadError(f"不支持的 payload 版本: {version!r}")

    raw_alerts = payload.get("alerts")
    if raw_alerts is None:
        raise PayloadError("payload 缺少 alerts 字段")
    if not isinstance(raw_alerts, list):
        raise PayloadError("alerts 必须是数组")

    group_key = str(payload.get("groupKey", ""))
    receiver = str(payload.get("receiver", ""))
    group_status = str(payload.get("status", ""))

    result: list[Alert] = []
    for idx, item in enumerate(raw_alerts):
        if not isinstance(item, dict):
            raise PayloadError(f"alerts[{idx}] 必须是对象")

        labels = _as_str_map(item.get("labels"), f"alerts[{idx}].labels")
        annotations = _as_str_map(item.get("annotations"), f"alerts[{idx}].annotations")

        alertname = labels.get("alertname", "")
        if not alertname:
            raise PayloadError(f"alerts[{idx}] 缺少 alertname label")

        fingerprint = str(item.get("fingerprint", "")) or _fallback_fingerprint(labels)

        result.append(
            Alert(
                fingerprint=fingerprint,
                alertname=alertname,
                # 单条 status 优先；缺失时退回组级 status
                status=str(item.get("status") or group_status or "firing"),
                severity=labels.get("severity", "unknown"),
                instance=labels.get("instance", ""),
                labels=labels,
                annotations=annotations,
                starts_at=parse_time(item.get("startsAt")),
                ends_at=parse_time(item.get("endsAt")),
                generator_url=str(item.get("generatorURL", "")),
                group_key=group_key,
                receiver=receiver,
            )
        )

    truncated = payload.get("truncatedAlerts") or 0
    if truncated:
        # 不抛错，但必须让上层知道有告警没发出来，否则会漏判影响面
        result.append(
            Alert(
                fingerprint=f"__truncated__{truncated}",
                alertname="__AlertmanagerTruncated__",
                status="firing",
                severity="warning",
                instance="",
                labels={"alertname": "__AlertmanagerTruncated__"},
                annotations={
                    "summary": f"Alertmanager 截断了 {truncated} 条告警，影响面可能被低估"
                },
                group_key=group_key,
                receiver=receiver,
            )
        )
    return result


def _fallback_fingerprint(labels: dict[str, str]) -> str:
    """payload 没带 fingerprint 时自行计算一个稳定哈希。

    注意：这不等于 Alertmanager 的 fingerprint 算法，只保证本地一致性，
    不能跨系统比对。
    """
    import hashlib

    canon = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return "local-" + hashlib.sha1(canon.encode()).hexdigest()[:16]


def normalize_many(
    payloads: list[dict[str, Any]], *, strict_version: bool = False
) -> list[Alert]:
    """多个 payload 合并归一化，并按 fingerprint 做状态合并。

    同一 fingerprint 多次出现时保留「最新」那条：
      - resolved 晚于 firing  → 以 resolved 为准
      - 都是 firing            → 以 startsAt 较晚者为准
    """
    merged: dict[str, Alert] = {}
    for payload in payloads:
        for alert in normalize(payload, strict_version=strict_version):
            old = merged.get(alert.fingerprint)
            if old is None or _is_newer(alert, old):
                merged[alert.fingerprint] = alert
    return list(merged.values())


def _is_newer(candidate: Alert, existing: Alert) -> bool:
    cand_t = candidate.ends_at or candidate.starts_at
    exist_t = existing.ends_at or existing.starts_at
    if cand_t and exist_t:
        return cand_t >= exist_t
    if cand_t and not exist_t:
        return True
    return False
