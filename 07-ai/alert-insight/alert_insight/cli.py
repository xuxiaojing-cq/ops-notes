"""命令行入口。

用法
----
    # 从文件分析（可传多个，会合并后一起聚合）
    python3 -m alert_insight.cli fixtures/*.json

    # 从 stdin 分析（接 Alertmanager webhook 时常用）
    cat payload.json | python3 -m alert_insight.cli -

    # 只要规则层结论，不调模型
    python3 -m alert_insight.cli --no-llm fixtures/storm.json

    # 给脚本用的 JSON 输出
    python3 -m alert_insight.cli --format json fixtures/storm.json

退出码
-----
    0  无告警 / 仅 info
    1  最高 warning
    2  最高 critical
    3  输入或参数错误（和「告警严重度」区分开，便于脚本判断）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Any

from .aggregate import aggregate
from .diagnose import diagnose
from .llm import LLMClient, LLMResult, enhance
from .normalize import PayloadError, normalize
from .render import exit_code, render_json, render_markdown

EXIT_INPUT_ERROR = 3


def _load_payloads(paths: list[str]) -> list[dict[str, Any]]:
    """读入一个或多个 payload。

    兼容两种形态：单个 webhook 对象，或多个对象组成的数组
    （便于把一段时间内收到的告警批量回放分析）。
    """
    payloads: list[dict[str, Any]] = []
    for path in paths:
        if path == "-":
            raw = sys.stdin.read()
            source = "<stdin>"
        else:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
            source = path

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayloadError(f"{source}: JSON 解析失败 - {exc}") from exc

        if isinstance(data, list):
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    raise PayloadError(f"{source}[{index}] 不是对象")
                payloads.append(item)
        elif isinstance(data, dict):
            payloads.append(data)
        else:
            raise PayloadError(f"{source}: 顶层必须是对象或数组")
    return payloads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alert-insight",
        description="Alertmanager 告警聚合与归因",
    )
    parser.add_argument("paths", nargs="+", help="payload 文件路径，`-` 表示从 stdin 读")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="输出格式")
    parser.add_argument(
        "--window", type=float, default=5.0, help="时间窗（分钟），同一窗口内的告警才可能被关联"
    )
    parser.add_argument(
        "--min-hosts", type=int, default=2, help="判定为共因所需的最少实例数"
    )
    parser.add_argument("--include-resolved", action="store_true", help="把已恢复的告警也纳入聚合")
    parser.add_argument("--no-flap-detect", action="store_true", help="关闭抖动检测")
    parser.add_argument("--no-llm", action="store_true", help="不调用模型，只输出规则层结论")
    parser.add_argument("--model", default=None, help="覆盖默认模型名")
    parser.add_argument("--llm-timeout", type=float, default=20.0, help="模型调用超时（秒）")
    parser.add_argument("--all-findings", action="store_true", help="展开全部候选根因")
    parser.add_argument(
        "--strict-version", action="store_true", help="遇到未知 payload 版本直接报错"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        payloads = _load_payloads(args.paths)
    except (OSError, PayloadError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    # 刻意不用 normalize_many：它按 fingerprint 合并，会把状态翻转抹掉，
    # 导致抖动永远检测不到。聚合层需要原始序列。
    alerts = []
    try:
        for payload in payloads:
            alerts.extend(normalize(payload, strict_version=args.strict_version))
    except PayloadError as exc:
        print(f"payload 结构错误：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    incidents = aggregate(
        alerts,
        window=_dt.timedelta(minutes=args.window),
        min_hosts_for_common_cause=args.min_hosts,
        include_resolved=args.include_resolved,
        detect_flaps=not args.no_flap_detect,
    )

    client: LLMClient | None = None
    if not args.no_llm:
        kwargs: dict[str, Any] = {"timeout": args.llm_timeout}
        if args.model:
            kwargs["model"] = args.model
        client = LLMClient(**kwargs)

    results: list[tuple[Any, Any, LLMResult | None]] = []
    for incident in incidents:
        findings = diagnose(incident)
        llm_result: LLMResult | None = None
        if client is not None:
            llm_result = enhance(
                incident.to_facts(),
                [f.to_dict() for f in findings],
                client=client,
            )
        results.append((incident, findings, llm_result))

    if args.format == "json":
        print(json.dumps(render_json(results), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(results, show_all_findings=args.all_findings))

    return exit_code(incidents)


if __name__ == "__main__":
    sys.exit(main())
