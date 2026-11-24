"""webhook 接收器：既能接 Alertmanager 实时告警，也能把原始 payload 落盘。

两个用途
-------
1. **抓真实数据。** 规则库要靠真实 payload 验证，不能只用构造数据。
   这个服务把收到的每个 payload 原样存成 JSON，之后可以用 cli 回放分析。
2. **实时分析。** 收到告警后直接跑聚合+归因，把结果打到 stdout（可被 journald 收集）。

为什么不用 Flask/FastAPI
---------------------
标准库 http.server 够用。目标机是 1.6G 内存的 ECS，告警链路上每多一个依赖
就多一个故障点。这个服务要比业务更不容易挂。

重要：它只监听回环地址
------------------
告警 payload 里有主机名、指标、业务信息。默认 127.0.0.1，
要暴露给同机的 Alertmanager 容器就用容器网络，不要直接 0.0.0.0。

用法
----
    python3 -m alert_insight.server --port 5001 --dump-dir ./captured

    # Alertmanager 侧
    receivers:
      - name: 'insight'
        webhook_configs:
          - url: 'http://127.0.0.1:5001/alert'
            send_resolved: true    # 默认是 false，不开就检测不到抖动
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .aggregate import aggregate
from .diagnose import diagnose
from .llm import LLMClient, enhance
from .normalize import PayloadError, normalize
from .render import render_markdown

MAX_BODY_BYTES = 4 * 1024 * 1024  # 4MB，超过直接拒绝，避免被撑爆内存

# 抖动检测需要跨 POST 的历史序列。
# 保留最近 N 秒内**收到**的 alert，超期的自动清理。
# 只用内存，不持久化——进程重启后重新学习，不影响当次 POST 的正常处理。
FLAP_WINDOW_SECONDS = 600  # 10 分钟
MAX_FLAP_HISTORY = 2000    # 条数上限，防无限增长

_LOCK = threading.Lock()
# 存 (接收时间, Alert)。**必须按接收时间过期，不能按 startsAt。**
# 第一版用 startsAt 判断超期，结果一条持续了几小时的告警刚推过来就被当成
# 「过期数据」清掉，聚合直接拿到空列表——实测时分析输出完全消失。
_flap_history: list[tuple[_dt.datetime, "Alert"]] = []


def _record_for_flap_detection(alerts: "list[Alert]") -> None:
    """把新到的告警追加进历史滑动窗口，超期的截掉。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(seconds=FLAP_WINDOW_SECONDS)
    with _LOCK:
        _flap_history.extend((now, a) for a in alerts)
        keep = [(ts, a) for ts, a in _flap_history if ts >= cutoff]
        _flap_history[:] = keep[-MAX_FLAP_HISTORY:]


def _snapshot_for_flap_detection() -> "list[Alert]":
    with _LOCK:
        return [a for _, a in _flap_history]


def _log(message: str) -> None:
    stamp = _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "alert-insight/0.1"

    # 这些由 server 主程序注入
    dump_dir: str | None = None
    analyze: bool = True
    use_llm: bool = False
    llm_client: LLMClient | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认实现往 stderr 打访问日志，这里统一走 _log 便于收集
        _log("http " + (fmt % args))

    def _reply(self, code: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/health"):
            self._reply(200, "ok")
            return
        self._reply(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/alert", "/alerts", ""):
            self._reply(404, "not found")
            return

        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self._reply(400, "invalid content-length")
            return

        if length <= 0:
            self._reply(400, "empty body")
            return
        if length > MAX_BODY_BYTES:
            self._reply(413, "body too large")
            return

        raw = self.rfile.read(length)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log(f"payload 解析失败: {exc}")
            # 对 Alertmanager 返回 200，否则它会不断重试同一条坏 payload。
            # 问题记在日志里，不要让重试风暴压垮接收端。
            self._reply(200, "stored-as-invalid")
            self._dump(raw, suffix="invalid")
            return

        self._dump(raw, suffix="payload")

        # Alertmanager 发的是单个对象；但回放抓取数据时常见「多个 payload 组成数组」，
        # 两种都要能吃。第一版只认对象，结果把自己的 fixture 判成非法输入。
        payloads = payload if isinstance(payload, list) else [payload]
        payloads = [p for p in payloads if isinstance(p, dict)]
        if not payloads:
            _log("payload 里没有可分析的对象，已存盘但跳过分析")
            self._reply(200, "ok")
            return

        for item in payloads:
            alerts_count = len(item.get("alerts") or [])
            _log(
                f"收到 payload: receiver={item.get('receiver')!r} "
                f"status={item.get('status')!r} alerts={alerts_count} "
                f"truncated={item.get('truncatedAlerts') or 0}"
            )
            if not self.analyze:
                continue
            try:
                self._analyze(item)
            except PayloadError as exc:
                _log(f"归一化失败: {exc}")
            except Exception as exc:  # pragma: no cover - 分析失败不能影响接收
                _log(f"分析异常（已忽略，payload 已存盘）: {type(exc).__name__}: {exc}")

        self._reply(200, "ok")

    def _analyze(self, payload: dict) -> None:
        alerts = normalize(payload)

        # 关键：实时场景下 Alertmanager 是一条一条推的，单次 POST 里看不到状态翻转。
        # 必须把当前 payload 并进历史窗口再分析，否则抖动永远检测不出来。
        # （第一版就是无状态分析，实测 web05 的 3 次翻转被拆成了 5 个独立事件。）
        _record_for_flap_detection(alerts)
        window = _snapshot_for_flap_detection()

        # 当前 payload 产生的事件仍然要单独输出——值班的人关心的是「刚发生了什么」。
        # 历史窗口只用于补充抖动判断，不改变本次事件的边界。
        incidents = aggregate(window)
        current_fps = {a.fingerprint for a in alerts}
        relevant = [
            i for i in incidents
            if any(a.fingerprint in current_fps for a in i.alerts)
        ]

        results = []
        for incident in relevant:
            findings = diagnose(incident)
            llm_result = None
            if self.use_llm and self.llm_client is not None:
                llm_result = enhance(
                    incident.to_facts(),
                    [f.to_dict() for f in findings],
                    client=self.llm_client,
                )
            results.append((incident, findings, llm_result))
        if results:
            print(render_markdown(results), flush=True)

    def _dump(self, raw: bytes, *, suffix: str) -> None:
        if not self.dump_dir:
            return
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = os.path.join(self.dump_dir, f"{stamp}-{suffix}.json")
        with _LOCK:
            try:
                with open(path, "wb") as handle:
                    handle.write(raw)
            except OSError as exc:
                _log(f"落盘失败 {path}: {exc}")
                return
        _log(f"已存盘 {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alert-insight-server", description="Alertmanager webhook 接收器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只听回环")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--dump-dir", default=None, help="原始 payload 落盘目录，用于后续回放分析")
    parser.add_argument("--no-analyze", action="store_true", help="只抓取不分析")
    parser.add_argument("--llm", action="store_true", help="实时分析时启用模型增强")
    args = parser.parse_args(argv)

    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)

    Handler.dump_dir = args.dump_dir
    Handler.analyze = not args.no_analyze
    Handler.use_llm = args.llm
    Handler.llm_client = LLMClient() if args.llm else None

    if args.llm and Handler.llm_client is not None and not Handler.llm_client.available:
        _log("已指定 --llm 但未配置 ANTHROPIC_API_KEY，将只输出规则层结论")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"监听 http://{args.host}:{args.port}/alert  落盘目录={args.dump_dir or '关闭'}  分析={Handler.analyze}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("收到中断，退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
