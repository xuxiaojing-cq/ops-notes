"""输出渲染：Markdown 事件卡片 / JSON，以及退出码。

退出码为什么重要
-------------
这个工具的使用场景不只是「人看一眼」，还要能被脚本和 CI 接住：
值班脚本可以根据退出码决定是否升级通知、是否阻断发布。
本仓库 05-cicd 的灰度闸门用的就是这个思路——用退出码表达决策，
才能嵌进自动化流程。

AI 内容必须可辨识
-------------
规则层结论和 LLM 结论在输出里**分块标注**，降级时显式写出原因。
让人随时知道眼前这段话是确定性规则给的，还是模型给的——
这两者的可信度完全不同，混在一起会出事。
"""

from __future__ import annotations

from .aggregate import SEVERITY_ORDER, Incident
from .diagnose import Finding
from .llm import LLMResult

EXIT_OK = 0
EXIT_WARNING = 1
EXIT_CRITICAL = 2

SEVERITY_ICON = {"critical": "🔴", "warning": "🟡", "info": "🔵", "unknown": "⚪"}

KIND_LABEL = {
    "common_cause": "共因（多实例同症状）",
    "single_host": "单实例多症状",
    "flapping": "告警抖动",
    "unscoped": "无实例标注",
}

CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}


def exit_code(incidents: list[Incident]) -> int:
    """按最高严重度决定退出码。"""
    worst = 0
    for incident in incidents:
        if not incident.firing_alerts and incident.kind != "flapping":
            continue
        worst = max(worst, SEVERITY_ORDER.get(incident.severity, 0))
    if worst >= SEVERITY_ORDER["critical"]:
        return EXIT_CRITICAL
    if worst >= SEVERITY_ORDER["warning"]:
        return EXIT_WARNING
    return EXIT_OK


def render_markdown(
    results: list[tuple[Incident, list[Finding], LLMResult | None]],
    *,
    show_all_findings: bool = False,
) -> str:
    """渲染成 Markdown。默认只展开置信度最高的候选，其余折叠成一行。"""
    if not results:
        return "## 告警分析\n\n没有需要处理的告警。\n"

    lines: list[str] = ["## 告警分析", ""]
    lines.append(f"聚合出 **{len(results)}** 个事件（按严重度与影响面排序）。")
    lines.append("")

    for index, (incident, findings, llm_result) in enumerate(results, start=1):
        icon = SEVERITY_ICON.get(incident.severity, "⚪")
        lines.append(f"### {index}. {icon} {incident.title}")
        lines.append("")

        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 事件类型 | {KIND_LABEL.get(incident.kind, incident.kind)} |")
        lines.append(f"| 严重度 | {incident.severity} |")
        if incident.instances:
            shown = ", ".join(incident.instances[:6])
            more = "…" if len(incident.instances) > 6 else ""
            lines.append(f"| 影响实例 | {len(incident.instances)} 个：{shown}{more} |")
        else:
            lines.append("| 影响实例 | 未标注 |")
        lines.append(f"| 告警种类 | {', '.join(incident.alertnames)} |")
        lines.append(
            f"| 告警条数 | 共 {len(incident.alerts)} 条，其中 firing {len(incident.firing_alerts)} 条 |"
        )
        if incident.started_at:
            lines.append(f"| 首次触发 | {incident.started_at.isoformat()} |")
        lines.append(f"| 事件 ID | `{incident.incident_id}` |")
        lines.append("")

        # ---------- LLM 解读（有就放前面，这是人最先看的） ----------
        if llm_result is not None:
            if llm_result.ok:
                lines.append(f"**🤖 AI 解读**（{llm_result.model}，{llm_result.latency_ms}ms）")
                lines.append("")
                lines.append(f"> {llm_result.headline}")
                lines.append("")
                if llm_result.reasoning:
                    lines.append(f"{llm_result.reasoning}")
                    lines.append("")
                if llm_result.next_commands:
                    lines.append("建议先执行：")
                    lines.append("")
                    lines.append("```bash")
                    for command in llm_result.next_commands:
                        lines.append(command)
                    lines.append("```")
                    lines.append("")
                if llm_result.uncertainty:
                    lines.append(f"⚠️ 尚不确定：{llm_result.uncertainty}")
                    lines.append("")
            else:
                lines.append(f"**🤖 AI 解读**：不可用，已降级。原因：{llm_result.degraded_reason}")
                lines.append("")
                lines.append("以下为确定性规则层结论，不含模型推断。")
                lines.append("")

        # ---------- 规则层 ----------
        primary = findings[:1] if not show_all_findings else findings
        for finding in primary:
            lines.extend(_render_finding(finding))

        rest = findings[1:] if not show_all_findings else []
        if rest:
            lines.append("<details><summary>其他候选根因（%d 条）</summary>" % len(rest))
            lines.append("")
            for finding in rest:
                lines.extend(_render_finding(finding))
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")

    lines.append(
        "> 根因均为**候选假设**，置信度来自规则匹配，不是统计概率。"
        "结论成立与否取决于「判别方法」里的命令实际跑出什么结果。"
    )
    lines.append("")
    return "\n".join(lines)


def _render_finding(finding: Finding) -> list[str]:
    out: list[str] = []
    label = CONFIDENCE_LABEL.get(finding.confidence, finding.confidence)
    out.append(f"**候选根因**（置信度：{label}，规则 `{finding.rule_id}`）")
    out.append("")
    out.append(finding.hypothesis)
    out.append("")

    if finding.evidence:
        out.append("支持证据：")
        for item in finding.evidence:
            out.append(f"- {item}")
        out.append("")

    if finding.counter_evidence:
        out.append("判别方法（如果不是这个原因，会看到什么）：")
        for item in finding.counter_evidence:
            out.append(f"- {item}")
        out.append("")

    if finding.verify:
        out.append("验证命令：")
        out.append("")
        out.append("```bash")
        for item in finding.verify:
            out.append(item)
        out.append("```")
        out.append("")

    if finding.actions:
        out.append("处置建议：")
        for item in finding.actions:
            out.append(f"- {item}")
        out.append("")

    if finding.note:
        out.append(f"💡 {finding.note}")
        out.append("")

    return out


def render_json(
    results: list[tuple[Incident, list[Finding], LLMResult | None]],
) -> dict:
    return {
        "incident_count": len(results),
        "exit_code": exit_code([incident for incident, _, _ in results]),
        "incidents": [
            {
                "facts": incident.to_facts(),
                "findings": [f.to_dict() for f in findings],
                "llm": llm_result.to_dict() if llm_result is not None else None,
            }
            for incident, findings, llm_result in results
        ],
    }
