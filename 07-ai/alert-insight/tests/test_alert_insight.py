"""alert-insight 单元测试。

运行：
    cd 07-ai/alert-insight && python3 -m unittest discover -s tests -v

重点测的不是「代码能跑」，而是几条**容易悄悄出错**的行为：
  - 抖动检测会不会被 fingerprint 去重抹掉
  - 采集侧告警会不会被误判成业务故障（方向性错误，比没结论更糟）
  - LLM 编造 rule_id 时校验层拦不拦得住
  - LLM 不可用时会不会静默降级（必须留下原因）
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alert_insight.aggregate import aggregate, detect_flapping  # noqa: E402
from alert_insight.diagnose import classify, diagnose  # noqa: E402
from alert_insight.llm import (  # noqa: E402
    LLMClient,
    _extract_json,
    _validate,
    build_prompt,
    enhance,
)
from alert_insight.normalize import (  # noqa: E402
    Alert,
    PayloadError,
    normalize,
    normalize_many,
    parse_time,
)
from alert_insight.render import EXIT_CRITICAL, EXIT_OK, EXIT_WARNING, exit_code  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def load_fixture(name: str):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def make_alert(
    *,
    alertname: str,
    instance: str = "host1:9100",
    severity: str = "warning",
    status: str = "firing",
    minute: int = 0,
    fingerprint: str | None = None,
    summary: str = "",
) -> Alert:
    return Alert(
        fingerprint=fingerprint or f"{alertname}-{instance}",
        alertname=alertname,
        status=status,
        severity=severity,
        instance=instance,
        labels={"alertname": alertname, "instance": instance, "severity": severity, "job": "node"},
        annotations={"summary": summary or alertname},
        starts_at=_dt.datetime(2026, 11, 17, 10, minute, 0, tzinfo=_dt.timezone.utc),
    )


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


class TestParseTime(unittest.TestCase):
    def test_firing_zero_endsat_is_none(self):
        """firing 时 endsAt 是零值，必须当成「还没结束」而不是 1 年的时间点。"""
        self.assertIsNone(parse_time("0001-01-01T00:00:00Z"))

    def test_empty_is_none(self):
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))

    def test_variable_fraction_digits(self):
        """Alertmanager 的小数秒位数不固定，两位也要能解析。"""
        two = parse_time("2026-09-20T14:09:38.18Z")
        six = parse_time("2026-09-20T14:09:38.180000Z")
        none_frac = parse_time("2026-09-20T14:09:38Z")
        self.assertIsNotNone(two)
        self.assertEqual(two, six)
        self.assertEqual(none_frac.replace(microsecond=0), two.replace(microsecond=0))

    def test_timezone_normalized_to_utc(self):
        shanghai = parse_time("2026-09-20T22:09:38+08:00")
        utc = parse_time("2026-09-20T14:09:38Z")
        self.assertEqual(shanghai, utc)


class TestNormalize(unittest.TestCase):
    def test_real_payload(self):
        """用 02-monitoring 笔记里记录的真实 payload 验证。"""
        payload = load_fixture("real-nodeexporter-down.json")[0]
        alerts = normalize(payload)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.alertname, "NodeExporterDown")
        self.assertEqual(alert.status, "resolved")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.instance, "localhost:9100")
        self.assertEqual(alert.fingerprint, "0c3f1299754f298a")
        self.assertEqual(alert.job, "node")
        self.assertEqual(alert.summary, "node_exporter 已停止")
        self.assertIsNotNone(alert.ends_at)  # resolved 有真实 endsAt
        self.assertIn('severity="critical"', alert.group_key)

    def test_missing_alerts_field_raises(self):
        with self.assertRaises(PayloadError):
            normalize({"version": "4"})

    def test_missing_alertname_raises(self):
        """没有 alertname 的告警无法归类，宁可报错也不要静默吞掉。"""
        with self.assertRaises(PayloadError):
            normalize({"version": "4", "alerts": [{"labels": {"instance": "x"}}]})

    def test_strict_version_rejects_unknown(self):
        payload = {"version": "99", "alerts": []}
        normalize(payload)  # 默认放行
        with self.assertRaises(PayloadError):
            normalize(payload, strict_version=True)

    def test_truncated_injects_marker(self):
        """截断必须显式暴露，否则影响面会被低估。"""
        payload = {
            "version": "4",
            "truncatedAlerts": 7,
            "alerts": [{"labels": {"alertname": "X"}, "status": "firing"}],
        }
        alerts = normalize(payload)
        names = [a.alertname for a in alerts]
        self.assertIn("__AlertmanagerTruncated__", names)
        marker = [a for a in alerts if a.alertname == "__AlertmanagerTruncated__"][0]
        self.assertIn("7", marker.summary)

    def test_status_falls_back_to_group_status(self):
        payload = {
            "version": "4",
            "status": "resolved",
            "alerts": [{"labels": {"alertname": "X"}}],
        }
        self.assertEqual(normalize(payload)[0].status, "resolved")

    def test_fallback_fingerprint_is_stable(self):
        payload = {"version": "4", "alerts": [{"labels": {"alertname": "X", "instance": "i"}}]}
        first = normalize(payload)[0].fingerprint
        second = normalize(payload)[0].fingerprint
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("local-"))

    def test_normalize_many_merges_by_fingerprint(self):
        """normalize_many 按 fingerprint 收敛到最新状态。"""
        payloads = load_fixture("synthetic-storm.json")
        merged = normalize_many(payloads)
        fingerprints = [a.fingerprint for a in merged]
        self.assertEqual(len(fingerprints), len(set(fingerprints)))
        web05 = [a for a in merged if a.fingerprint == "bb00000000000031"]
        self.assertEqual(len(web05), 1)
        self.assertEqual(web05[0].status, "resolved")  # 最后一次是 resolved


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


class TestFlapping(unittest.TestCase):
    def test_detects_flips(self):
        seq = []
        for i, status in enumerate(["firing", "resolved", "firing", "resolved", "firing"]):
            seq.append(make_alert(alertname="MemHigh", status=status, minute=i, fingerprint="fp-1"))
        flaps = detect_flapping(seq)
        self.assertEqual(flaps["fp-1"], 4)

    def test_stable_alert_not_flagged(self):
        seq = [make_alert(alertname="MemHigh", minute=i, fingerprint="fp-1") for i in range(5)]
        self.assertEqual(detect_flapping(seq), {})

    def test_dedup_before_detection_hides_flapping(self):
        """这是本项目最容易踩的坑：先按 fingerprint 去重，抖动就永远测不出来。

        所以 cli 刻意不用 normalize_many，而是把原始序列交给 aggregate。
        """
        payloads = load_fixture("synthetic-storm.json")
        raw: list[Alert] = []
        for payload in payloads:
            raw.extend(normalize(payload))

        self.assertTrue(detect_flapping(raw), "原始序列应能检测到抖动")
        self.assertEqual(
            detect_flapping(normalize_many(payloads)),
            {},
            "去重后的序列检测不到抖动——这正是不能先去重的原因",
        )


class TestAggregate(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(aggregate([]), [])

    def test_common_cause_across_instances(self):
        alerts = [
            make_alert(alertname="ServiceDown", instance=f"web0{i}:8080", severity="critical", minute=i)
            for i in range(1, 4)
        ]
        incidents = aggregate(alerts)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].kind, "common_cause")
        self.assertEqual(len(incidents[0].instances), 3)

    def test_single_host_multiple_symptoms(self):
        alerts = [
            make_alert(alertname="HighLoadAverage", instance="db01:9100", minute=0),
            make_alert(alertname="HighDiskIOWait", instance="db01:9100", minute=1),
        ]
        incidents = aggregate(alerts)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].kind, "single_host")
        self.assertEqual(sorted(incidents[0].alertnames), ["HighDiskIOWait", "HighLoadAverage"])

    def test_no_false_common_cause_when_companions_differ(self):
        """回归测试：只凭「同一个 alertname 出现在多个实例上」就判共因是错的。

        实测暴露的场景：db01 是「负载 + IO 等待」，web04 是「采集挂了 + 负载 + 内存」。
        两台机器唯一的共同点只是都有 HighLoadAverage，却是两个不同的故障。
        第一版把它们合并成假共因后，db01 的 IO 瓶颈规则（R003）永远命中不了。
        """
        alerts = [
            make_alert(alertname="HighLoadAverage", instance="db01:9100", minute=0),
            make_alert(alertname="HighDiskIOWait", instance="db01:9100", minute=0),
            make_alert(alertname="HighLoadAverage", instance="web04:9100", minute=1),
            make_alert(alertname="NodeExporterDown", instance="web04:9100", severity="critical", minute=1),
            make_alert(alertname="HighMemoryUsage", instance="web04:9100", minute=1),
        ]
        incidents = aggregate(alerts)

        self.assertTrue(
            all(i.kind != "common_cause" for i in incidents),
            "剩余告警种类不一致的实例不应被合并成共因",
        )

        # 关键断言：db01 的 IO 瓶颈还必须能被诊断出来
        db01 = [i for i in incidents if i.instances == ["db01:9100"]]
        self.assertEqual(len(db01), 1)
        ids = [f.rule_id for f in diagnose(db01[0])]
        self.assertIn("R003-load-io-bound", ids)

    def test_true_common_cause_still_detected(self):
        """真的共因（实例上只有同一个告警）不能被上面的修复误伤。"""
        alerts = [
            make_alert(alertname="ServiceDown", instance=f"web0{i}:8080", severity="critical", minute=0)
            for i in range(1, 4)
        ]
        incidents = aggregate(alerts)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].kind, "common_cause")

    def test_identical_companions_still_common_cause(self):
        """各实例告警组合完全一致时仍算共因——更像同一套变更下发。"""
        alerts = [
            make_alert(alertname="HighLoadAverage", instance=f"h{i}:9100", minute=0)
            for i in range(1, 3)
        ] + [
            make_alert(alertname="HighDiskIOWait", instance=f"h{i}:9100", minute=0)
            for i in range(1, 3)
        ]
        incidents = aggregate(alerts)
        kinds = {i.kind for i in incidents}
        self.assertIn("common_cause", kinds)

    def test_spread_seconds(self):
        alerts = [
            make_alert(alertname="A", instance="h1:1", minute=0),
            make_alert(alertname="B", instance="h1:1", minute=2),
        ]
        self.assertEqual(aggregate(alerts)[0].spread_seconds, 120.0)

    def test_spread_seconds_single_alert(self):
        self.assertIsNone(aggregate([make_alert(alertname="A", instance="h:1")])[0].spread_seconds)


        alerts = [
            make_alert(alertname="ServiceDown", instance="web01:8080", minute=0),
            make_alert(alertname="ServiceDown", instance="web02:8080", minute=1),
        ]
        two = aggregate(alerts, min_hosts_for_common_cause=2)
        self.assertEqual(two[0].kind, "common_cause")

        three = aggregate(alerts, min_hosts_for_common_cause=3)
        self.assertTrue(all(i.kind != "common_cause" for i in three))

    def test_time_window_splits_unrelated_alerts(self):
        """相隔很久的同名告警不应被当成一个共因事件。"""
        alerts = [
            make_alert(alertname="ServiceDown", instance="web01:8080", minute=0),
            make_alert(alertname="ServiceDown", instance="web02:8080", minute=50),
        ]
        incidents = aggregate(alerts, window=_dt.timedelta(minutes=5))
        self.assertTrue(all(i.kind != "common_cause" for i in incidents))

    def test_resolved_excluded_by_default(self):
        alerts = [make_alert(alertname="MemHigh", status="resolved", minute=0)]
        self.assertEqual(aggregate(alerts, detect_flaps=False), [])
        self.assertEqual(len(aggregate(alerts, include_resolved=True, detect_flaps=False)), 1)

    def test_severity_is_worst_in_incident(self):
        alerts = [
            make_alert(alertname="A", instance="h:1", severity="warning", minute=0),
            make_alert(alertname="B", instance="h:1", severity="critical", minute=0),
        ]
        self.assertEqual(aggregate(alerts)[0].severity, "critical")

    def test_sorted_by_severity_then_blast_radius(self):
        alerts = [
            make_alert(alertname="Minor", instance="h1:1", severity="warning", minute=0),
            make_alert(alertname="Major", instance="h2:1", severity="critical", minute=0),
        ]
        incidents = aggregate(alerts)
        self.assertEqual(incidents[0].severity, "critical")

    def test_storm_fixture_shape(self):
        payloads = load_fixture("synthetic-storm.json")
        raw: list[Alert] = []
        for payload in payloads:
            raw.extend(normalize(payload))
        incidents = aggregate(raw)
        kinds = {i.kind for i in incidents}
        self.assertIn("common_cause", kinds)
        self.assertIn("flapping", kinds)
        # 几十条告警应被压成个位数事件，这是「降维」的核心价值
        self.assertLess(len(incidents), len(raw))

    def test_storm_fixture_has_no_false_common_cause(self):
        """storm fixture 里 db01 与 web04 都触发 HighLoadAverage，但不能被合并成共因。"""
        payloads = load_fixture("synthetic-storm.json")
        raw: list[Alert] = []
        for payload in payloads:
            raw.extend(normalize(payload))
        for incident in aggregate(raw):
            if incident.alertnames == ["HighLoadAverage"]:
                self.fail(f"出现了假的负载共因：{incident.instances}")

    def test_storm_fixture_db01_gets_io_diagnosis(self):
        """db01 的「负载 + IO」应命中 IO 瓶颈规则，而不是「没有规则命中」。"""
        payloads = load_fixture("synthetic-storm.json")
        raw: list[Alert] = []
        for payload in payloads:
            raw.extend(normalize(payload))
        db01 = [i for i in aggregate(raw) if i.instances == ["db01:9100"]]
        self.assertEqual(len(db01), 1, "db01 的告警应聚成一个事件")
        ids = [f.rule_id for f in diagnose(db01[0])]
        self.assertIn("R003-load-io-bound", ids)
        self.assertNotIn("R000-no-rule-matched", ids)

    def test_truncated_title_is_readable(self):
        """内部标记名不应该原样出现在标题里。"""
        payload = {
            "version": "4",
            "truncatedAlerts": 2,
            "alerts": [{"labels": {"alertname": "X"}, "status": "firing"}],
        }
        incidents = aggregate(normalize(payload))
        marker = [i for i in incidents if any("Truncated" in n for n in i.alertnames)]
        self.assertTrue(marker)
        self.assertNotIn("__AlertmanagerTruncated__", marker[0].title)


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


class TestClassify(unittest.TestCase):
    def test_scrape_down_not_treated_as_service_down(self):
        """方向性错误最危险：采集挂了被当成业务挂了，会让人去重启业务。"""
        symptoms = classify(make_alert(alertname="NodeExporterDown"))
        self.assertIn("scrape_down", symptoms)
        self.assertNotIn("service_down", symptoms)

    def test_naming_variants_map_to_same_symptom(self):
        for name in ("HighLoadAverage", "node_load_high", "LoadTooHigh"):
            self.assertIn("load", classify(make_alert(alertname=name)), name)

    def test_summary_participates_in_classification(self):
        alert = make_alert(alertname="Custom001", summary="磁盘 iowait 持续偏高")
        self.assertIn("disk_io", classify(alert))


class TestDiagnose(unittest.TestCase):
    def test_scrape_side_rule_ranks_first(self):
        alerts = [
            make_alert(alertname="NodeExporterDown", instance="web04:9100", severity="critical", minute=0),
            make_alert(alertname="HighLoadAverage", instance="web04:9100", minute=0),
            make_alert(alertname="HighMemoryUsage", instance="web04:9100", minute=0),
        ]
        incident = aggregate(alerts)[0]
        findings = diagnose(incident)
        self.assertEqual(findings[0].rule_id, "R001-scrape-side-failure")
        self.assertEqual(findings[0].confidence, "high")
        self.assertTrue(findings[0].verify)
        self.assertTrue(findings[0].counter_evidence)

    def test_common_cause_rule(self):
        alerts = [
            make_alert(alertname="ServiceDown", instance=f"web0{i}:8080", severity="critical", minute=0)
            for i in range(1, 4)
        ]
        incident = aggregate(alerts)[0]
        ids = [f.rule_id for f in diagnose(incident)]
        self.assertIn("R002-common-cause", ids)
        # 共因场景不该再给「单实例服务不可用」的结论，否则排查方向矛盾
        self.assertNotIn("R007-service-down-single", ids)

    def test_io_bound_rule(self):
        alerts = [
            make_alert(alertname="HighLoadAverage", instance="db01:9100", minute=0),
            make_alert(alertname="HighDiskIOWait", instance="db01:9100", minute=0),
        ]
        incident = aggregate(alerts)[0]
        findings = diagnose(incident)
        ids = [f.rule_id for f in findings]
        self.assertIn("R003-load-io-bound", ids)
        io = [f for f in findings if f.rule_id == "R003-load-io-bound"][0]
        self.assertEqual(io.confidence, "high")
        self.assertTrue(any("D" in v for v in io.verify), "应给出查 D 状态进程的命令")

    def test_load_only_rule_is_not_overconfident(self):
        incident = aggregate([make_alert(alertname="HighLoadAverage", instance="db01:9100")])[0]
        findings = [f for f in diagnose(incident) if f.rule_id == "R004-load-without-cpu-io"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].confidence, "medium")

    def test_flapping_rule(self):
        seq = [
            make_alert(alertname="HighMemoryUsage", status=s, minute=i, fingerprint="fp-1")
            for i, s in enumerate(["firing", "resolved", "firing", "resolved"])
        ]
        incidents = [i for i in aggregate(seq) if i.kind == "flapping"]
        self.assertEqual(len(incidents), 1)
        ids = [f.rule_id for f in diagnose(incidents[0])]
        self.assertIn("R008-flapping", ids)

    def test_unknown_alert_admits_ignorance(self):
        """没有规则命中时必须诚实说不知道，不能硬凑结论。"""
        incident = aggregate([make_alert(alertname="SomethingNobodyModeled")])[0]
        findings = diagnose(incident)
        self.assertEqual(findings[0].rule_id, "R000-no-rule-matched")
        self.assertEqual(findings[0].confidence, "low")
        self.assertIn("无法", findings[0].hypothesis)

    def test_every_finding_has_verify_or_is_unknown(self):
        """规则给结论必须同时给判别方法，否则人没法降低不确定性。"""
        cases = [
            [make_alert(alertname="NodeExporterDown", severity="critical")],
            [make_alert(alertname="HighMemoryUsage"), make_alert(alertname="ServiceDown", instance="h:1")],
            [make_alert(alertname="DiskSpaceLow", severity="critical")],
            [make_alert(alertname="CertExpiringSoon")],
            [make_alert(alertname="HighErrorRate")],
        ]
        for alerts in cases:
            for incident in aggregate(alerts):
                for finding in diagnose(incident):
                    self.assertTrue(
                        finding.verify or finding.rule_id.startswith("R000"),
                        f"{finding.rule_id} 给了结论却没给验证方法",
                    )

    def test_truncated_rule_triggers(self):
        payload = {
            "version": "4",
            "truncatedAlerts": 5,
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "ServiceDown", "instance": "a:1", "severity": "critical"},
                    "startsAt": "2026-11-17T10:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": "t1",
                }
            ],
        }
        alerts = normalize(payload)
        ids = {f.rule_id for incident in aggregate(alerts) for f in diagnose(incident)}
        self.assertIn("R009-truncated", ids)


# ---------------------------------------------------------------------------
# llm：只测本地逻辑，不发网络请求
# ---------------------------------------------------------------------------


class TestLLMValidation(unittest.TestCase):
    ALLOWED = {"R002-common-cause", "R007-service-down-single"}

    def test_rejects_fabricated_rule_id(self):
        """模型编造候选之外的 rule_id —— 这是幻觉的主要表现形式，必须拦住。"""
        ok, reason = _validate(
            {"headline": "x", "ranked_rule_ids": ["R999-i-made-this-up"]}, self.ALLOWED
        )
        self.assertFalse(ok)
        self.assertIn("候选之外", reason)

    def test_accepts_valid_output(self):
        ok, reason = _validate(
            {
                "headline": "三台实例同时不可用，疑似发布导致",
                "ranked_rule_ids": ["R002-common-cause"],
                "reasoning": "触发时间集中在 8 秒内",
                "next_commands": ["docker ps -a"],
                "uncertainty": "尚未确认是否刚发布",
            },
            self.ALLOWED,
        )
        self.assertTrue(ok, reason)

    def test_rejects_missing_headline(self):
        ok, _ = _validate({"ranked_rule_ids": ["R002-common-cause"]}, self.ALLOWED)
        self.assertFalse(ok)

    def test_rejects_empty_ranking(self):
        ok, _ = _validate({"headline": "x", "ranked_rule_ids": []}, self.ALLOWED)
        self.assertFalse(ok)

    def test_rejects_wrong_types(self):
        ok, _ = _validate(
            {"headline": "x", "ranked_rule_ids": ["R002-common-cause"], "next_commands": "not-a-list"},
            self.ALLOWED,
        )
        self.assertFalse(ok)


class TestExtractJSON(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_leading_chatter(self):
        self.assertEqual(_extract_json('好的，分析如下：\n{"a": 1}\n希望有帮助'), {"a": 1})

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            _extract_json("完全没有 JSON 的一段话")


class TestLLMDegrade(unittest.TestCase):
    def test_no_api_key_degrades_with_reason(self):
        """降级必须留下原因。静默降级会让人误以为模型认可了规则结论。"""
        client = LLMClient(api_key="", base_url="http://127.0.0.1:1")
        result = enhance(
            {"incident_id": "x"},
            [{"rule_id": "R002-common-cause", "hypothesis": "h", "confidence": "high",
              "evidence": [], "counter_evidence": [], "verify": []}],
            client=client,
        )
        self.assertFalse(result.ok)
        self.assertIn("ANTHROPIC_API_KEY", result.degraded_reason)

    def test_no_candidates_skips_llm(self):
        result = enhance({"incident_id": "x"}, [], client=LLMClient(api_key="dummy"))
        self.assertFalse(result.ok)
        self.assertIn("没有候选", result.degraded_reason)

    def test_network_failure_degrades_not_raises(self):
        """网络不可达时不能抛异常——告警通道不能因为模型挂了而中断。"""
        client = LLMClient(api_key="dummy", base_url="http://127.0.0.1:1", timeout=0.5)
        result = enhance(
            {"incident_id": "x"},
            [{"rule_id": "R002-common-cause", "hypothesis": "h", "confidence": "high",
              "evidence": [], "counter_evidence": [], "verify": []}],
            client=client,
            retries=0,
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.degraded_reason)


class TestPrompt(unittest.TestCase):
    def test_prompt_contains_allowed_ids(self):
        findings = [
            {"rule_id": "R002-common-cause", "hypothesis": "h", "confidence": "high",
             "evidence": ["e"], "counter_evidence": ["c"], "verify": ["v"]}
        ]
        prompt = build_prompt({"incident_id": "abc"}, findings)
        self.assertIn("allowed_rule_ids", prompt)
        self.assertIn("R002-common-cause", prompt)

    def test_prompt_does_not_leak_actions(self):
        """actions 是给人看的处置建议，不进 prompt——减少模型照抄的空间。"""
        findings = [
            {"rule_id": "R002-common-cause", "hypothesis": "h", "confidence": "high",
             "evidence": [], "counter_evidence": [], "verify": [], "actions": ["SECRET_ACTION"]}
        ]
        self.assertNotIn("SECRET_ACTION", build_prompt({}, findings))


# ---------------------------------------------------------------------------
# render / exit code
# ---------------------------------------------------------------------------


class TestExitCode(unittest.TestCase):
    def test_no_incidents(self):
        self.assertEqual(exit_code([]), EXIT_OK)

    def test_warning(self):
        incidents = aggregate([make_alert(alertname="HighLoadAverage", severity="warning")])
        self.assertEqual(exit_code(incidents), EXIT_WARNING)

    def test_critical(self):
        incidents = aggregate([make_alert(alertname="ServiceDown", severity="critical", instance="h:1")])
        self.assertEqual(exit_code(incidents), EXIT_CRITICAL)

    def test_info_only_is_ok(self):
        incidents = aggregate([make_alert(alertname="SomeNotice", severity="info")])
        self.assertEqual(exit_code(incidents), EXIT_OK)


class TestRenderMarkdown(unittest.TestCase):
    def test_degraded_reason_is_visible(self):
        """降级原因必须出现在输出里，让人知道这段话不是模型给的。"""
        from alert_insight.llm import LLMResult
        from alert_insight.render import render_markdown

        incident = aggregate([make_alert(alertname="ServiceDown", severity="critical", instance="h:1")])[0]
        findings = diagnose(incident)
        degraded = LLMResult(ok=False, degraded_reason="测试用降级原因")
        output = render_markdown([(incident, findings, degraded)])
        self.assertIn("测试用降级原因", output)
        self.assertIn("规则层", output)

    def test_renders_verify_commands(self):
        from alert_insight.render import render_markdown

        incident = aggregate([make_alert(alertname="DiskSpaceLow", severity="critical", instance="h:1")])[0]
        output = render_markdown([(incident, diagnose(incident), None)])
        self.assertIn("df -h", output)
        self.assertIn("判别方法", output)


# ---------------------------------------------------------------------------
# cli 端到端
# ---------------------------------------------------------------------------


class TestCLI(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import io

        from alert_insight.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_storm_markdown_no_llm(self):
        path = os.path.join(FIXTURES, "synthetic-storm.json")
        code, output = self._run(["--no-llm", path])
        self.assertEqual(code, EXIT_CRITICAL)
        self.assertIn("告警分析", output)
        self.assertIn("共因", output)

    def test_storm_json_no_llm(self):
        path = os.path.join(FIXTURES, "synthetic-storm.json")
        code, output = self._run(["--no-llm", "--format", "json", path])
        self.assertEqual(code, EXIT_CRITICAL)
        data = json.loads(output)
        self.assertGreater(data["incident_count"], 0)
        self.assertEqual(data["exit_code"], EXIT_CRITICAL)
        for incident in data["incidents"]:
            self.assertTrue(incident["findings"], "每个事件都必须有归因结论")

    def test_real_fixture_resolved_only(self):
        """只有一条 resolved 告警时不该报故障。"""
        path = os.path.join(FIXTURES, "real-nodeexporter-down.json")
        code, _ = self._run(["--no-llm", path])
        self.assertEqual(code, EXIT_OK)

    def test_bad_input_returns_input_error_code(self):
        """输入错误用独立退出码，不能和「告警严重度」混淆。"""
        from alert_insight.cli import EXIT_INPUT_ERROR

        code, _ = self._run(["--no-llm", os.path.join(FIXTURES, "does-not-exist.json")])
        self.assertEqual(code, EXIT_INPUT_ERROR)


class TestServerFlapWindow(unittest.TestCase):
    """接收器的跨请求抖动检测。

    这块是实测暴露的问题：Alertmanager 一次推一条，无状态分析时
    web05 的 3 次翻转被拆成 5 个独立事件，抖动永远识别不出来。
    """

    def setUp(self):
        from alert_insight import server

        server._flap_history.clear()

    def tearDown(self):
        from alert_insight import server

        server._flap_history.clear()

    def test_history_accumulates_across_posts(self):
        from alert_insight import server

        server._record_for_flap_detection([make_alert(alertname="A", fingerprint="f1")])
        server._record_for_flap_detection([make_alert(alertname="A", status="resolved", fingerprint="f1")])
        self.assertEqual(len(server._snapshot_for_flap_detection()), 2)

    def test_old_starts_at_is_not_discarded(self):
        """回归测试：过期判断必须用接收时间，不能用告警的 startsAt。

        第一版按 startsAt 清理，一条持续几小时的告警刚推来就被当过期数据丢掉，
        聚合拿到空列表、分析输出直接消失。
        """
        from alert_insight import server

        # startsAt 是「2020 年」，但刚刚收到（Alert 是 frozen 的，用 replace 构造）
        import dataclasses

        old = dataclasses.replace(
            make_alert(alertname="A", fingerprint="f1"),
            starts_at=_dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc),
        )
        server._record_for_flap_detection([old])

        self.assertEqual(len(server._snapshot_for_flap_detection()), 1)

    def test_flapping_detected_from_incremental_posts(self):
        """逐条推送也能攒出抖动结论——这是接收器的核心行为。"""
        from alert_insight import server

        for status in ("firing", "resolved", "firing", "resolved"):
            server._record_for_flap_detection(
                [make_alert(alertname="HighMemoryUsage", status=status, fingerprint="fp-1")]
            )

        incidents = [i for i in aggregate(server._snapshot_for_flap_detection()) if i.kind == "flapping"]
        self.assertEqual(len(incidents), 1)
        ids = [f.rule_id for f in diagnose(incidents[0])]
        self.assertIn("R008-flapping", ids)

    def test_history_is_bounded(self):
        from alert_insight import server

        for _ in range(server.MAX_FLAP_HISTORY + 50):
            server._record_for_flap_detection([make_alert(alertname="A", fingerprint="f1")])
        self.assertLessEqual(len(server._snapshot_for_flap_detection()), server.MAX_FLAP_HISTORY)


class TestServerPayloadShapes(unittest.TestCase):
    def test_accepts_list_payload(self):
        """回归测试：接收器必须同时吃「单个对象」和「数组」。

        第一版只认对象，把自己的 fixture（数组）判成「顶层不是对象」跳过分析。
        """
        from alert_insight.server import Handler

        payload = load_fixture("synthetic-common-cause.json")
        self.assertIsInstance(payload, list)
        # 只验证归一化路径能处理列表元素
        for item in payload:
            self.assertGreater(len(normalize(item)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
