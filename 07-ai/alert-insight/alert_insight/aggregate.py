"""告警聚合：把一堆零散告警聚成「事件（incident）」。

为什么必须先聚合再给 LLM
----------------------
告警风暴时一次几百条，直接丢给模型有三个必死问题：token 爆炸、延迟不可控、
模型在长列表里更容易编造关联。聚合的作用是**确定性降维**——几百条变几个事件，
LLM 只需要对少量结构化事实做自然语言表达。

两个方向的关联
------------
纵向（同一台机器多个指标异常）：
    web01 上同时 HighLoad + DiskIOWait + ServiceDown
    → 大概率是同一个根因在不同层面的表现

横向（同一指标在多台机器同时异常）：
    web01/web02/web03 同时 ServiceDown
    → 不是三台机器各自坏了，而是共同上游（网关、依赖服务、刚发布的版本）

**横向优先。** 对 To B 批量交付场景，「同一告警在多个机构同时出现」几乎总是
版本级或平台级共因，而「单机器多指标」往往是那台机器自己的问题。
把横向识别出来能直接避免「按机构逐个排查」这种最浪费时间的做法。

时间窗的取舍
----------
窗口太小切散真实事件，太大把无关告警糊在一起。本模块用「相邻间隔聚类」而不是
固定时刻对齐的桶，避免一个事件恰好跨桶边界被劈成两半。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field

from .normalize import Alert

SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1, "unknown": 0}

DEFAULT_WINDOW = _dt.timedelta(minutes=5)
DEFAULT_MIN_HOSTS_FOR_COMMON_CAUSE = 2


@dataclass
class Incident:
    """一个聚合后的事件。"""

    incident_id: str
    kind: str  # common_cause | single_host | flapping | unscoped
    alerts: list[Alert] = field(default_factory=list)
    flap_counts: dict[str, int] = field(default_factory=dict)

    # ---------- 派生属性 ----------
    @property
    def instances(self) -> list[str]:
        return sorted({a.instance for a in self.alerts if a.instance})

    @property
    def alertnames(self) -> list[str]:
        return sorted({a.alertname for a in self.alerts})

    @property
    def jobs(self) -> list[str]:
        return sorted({a.job for a in self.alerts if a.job})

    @property
    def severity(self) -> str:
        if not self.alerts:
            return "unknown"
        return max(
            (a.severity for a in self.alerts),
            key=lambda s: SEVERITY_ORDER.get(s, 0),
        )

    @property
    def firing_alerts(self) -> list[Alert]:
        return [a for a in self.alerts if a.status == "firing"]

    @property
    def started_at(self) -> _dt.datetime | None:
        times = [a.starts_at for a in self.alerts if a.starts_at]
        return min(times) if times else None

    @property
    def last_seen(self) -> _dt.datetime | None:
        times = [t for a in self.alerts for t in (a.starts_at, a.ends_at) if t]
        return max(times) if times else None

    @property
    def spread_seconds(self) -> float | None:
        """组内告警首次触发时间的跨度（秒）。

        真共因的触发时间通常挤在几秒到几十秒内；跨度很大说明这些告警
        只是「在同一聚合窗口里被一起发现」，不一定是同一件事。
        归因层用它来调低置信度，而不是硬判。
        """
        starts = [a.starts_at for a in self.alerts if a.starts_at]
        if len(starts) < 2:
            return None
        return (max(starts) - min(starts)).total_seconds()

    @property
    def title(self) -> str:
        names = self.alertnames
        if self.kind == "common_cause":
            head = names[0] if len(names) == 1 else f"{len(names)} 类告警"
            return f"{head} 同时影响 {len(self.instances)} 个实例"
        if self.kind == "flapping":
            worst = max(self.flap_counts.items(), key=lambda kv: kv[1], default=("", 0))
            return f"告警抖动：{names[0] if names else '未知'}（{worst[1]} 次状态翻转）"
        if self.kind == "single_host":
            target = self.instances[0] if self.instances else "未知实例"
            return f"{target} 出现 {len(names)} 类告警"
        # unscoped：内部标记名（__AlertmanagerTruncated__）不该原样露给值班的人
        head = _display_name(names[0]) if names else "未命名告警"
        return f"{head}（{len(self.alerts)} 条，无实例标注）"

    def to_facts(self) -> dict:
        """导出给归因层/LLM 用的结构化事实。

        只放「从 payload 里直接读到的东西」，不含任何推断，
        这样 LLM 的输入边界是明确的，便于约束它不要凭空发挥。
        """
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "title": self.title,
            "severity": self.severity,
            "instance_count": len(self.instances),
            "instances": self.instances,
            "alertnames": self.alertnames,
            "jobs": self.jobs,
            "firing_count": len(self.firing_alerts),
            "total_count": len(self.alerts),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "spread_seconds": self.spread_seconds,
            "flap_counts": dict(self.flap_counts),
            "alerts": [
                {
                    "alertname": a.alertname,
                    "instance": a.instance,
                    "severity": a.severity,
                    "status": a.status,
                    "summary": a.summary,
                    "labels": a.labels,
                }
                for a in self.alerts
            ],
        }


def _incident_id(kind: str, keys: list[str]) -> str:
    canon = kind + "|" + "|".join(sorted(keys))
    return hashlib.sha1(canon.encode()).hexdigest()[:12]


# 内部标记告警名 → 展示用名称
_DISPLAY_NAMES = {
    "__AlertmanagerTruncated__": "Alertmanager 截断标记",
}


def _display_name(alertname: str) -> str:
    return _DISPLAY_NAMES.get(alertname, alertname)


def detect_flapping(alerts: list[Alert], *, min_flips: int = 3) -> dict[str, int]:
    """统计每个 fingerprint 的状态翻转次数。

    抖动是运维里最该被单独拎出来的信号：既不是「坏了」也不是「好了」，
    按普通告警处理会被 resolved 掩盖掉，但它通常意味着资源临界或探针配置不当。

    注意：本函数依赖**未经 fingerprint 合并的原始序列**。
    如果上游已经用 normalize_many 合并过，这里永远检测不到抖动。
    """
    seq: dict[str, list[str]] = defaultdict(list)
    for a in sorted(alerts, key=lambda x: (x.starts_at or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))):
        seq[a.fingerprint].append(a.status)

    flaps: dict[str, int] = {}
    for fp, statuses in seq.items():
        flips = sum(1 for i in range(1, len(statuses)) if statuses[i] != statuses[i - 1])
        if flips >= min_flips:
            flaps[fp] = flips
    return flaps


def _time_cluster(alerts: list[Alert], window: _dt.timedelta) -> list[list[Alert]]:
    """按相邻间隔聚类。无时间戳的告警单独归入最后一簇，不丢弃。"""
    timed = [a for a in alerts if a.starts_at]
    untimed = [a for a in alerts if not a.starts_at]

    timed.sort(key=lambda a: a.starts_at)  # type: ignore[arg-type,return-value]
    clusters: list[list[Alert]] = []
    for alert in timed:
        if clusters and alert.starts_at - clusters[-1][-1].starts_at <= window:  # type: ignore[operator]
            clusters[-1].append(alert)
        else:
            clusters.append([alert])

    if untimed:
        clusters.append(untimed)
    return clusters


def aggregate(
    alerts: list[Alert],
    *,
    window: _dt.timedelta = DEFAULT_WINDOW,
    min_hosts_for_common_cause: int = DEFAULT_MIN_HOSTS_FOR_COMMON_CAUSE,
    include_resolved: bool = False,
    detect_flaps: bool = True,
) -> list[Incident]:
    """聚合入口。

    include_resolved=False 时只对 firing 做事件聚合，但抖动检测仍会用到
    resolved 记录（否则无法发现翻转）。
    """
    if not alerts:
        return []

    incidents: list[Incident] = []

    # ---------- 抖动：独立于时间窗，先摘出来 ----------
    flapping_fps: set[str] = set()
    if detect_flaps:
        flaps = detect_flapping(alerts)
        for fp, flips in flaps.items():
            members = [a for a in alerts if a.fingerprint == fp]
            incidents.append(
                Incident(
                    incident_id=_incident_id("flapping", [fp]),
                    kind="flapping",
                    alerts=members,
                    flap_counts={fp: flips},
                )
            )
            flapping_fps.add(fp)

    pool = [a for a in alerts if a.fingerprint not in flapping_fps]
    if not include_resolved:
        pool = [a for a in pool if a.status == "firing"]

    # 同 fingerprint 去重，只留一条代表（抖动已单独处理）
    deduped: dict[str, Alert] = {}
    for a in pool:
        deduped.setdefault(a.fingerprint, a)
    pool = list(deduped.values())

    for cluster in _time_cluster(pool, window):
        incidents.extend(
            _split_cluster(cluster, min_hosts_for_common_cause=min_hosts_for_common_cause)
        )

    # 严重度高的、影响面大的排前面
    incidents.sort(
        key=lambda i: (
            SEVERITY_ORDER.get(i.severity, 0),
            len(i.instances),
            len(i.alerts),
        ),
        reverse=True,
    )
    return incidents


def _split_cluster(
    cluster: list[Alert], *, min_hosts_for_common_cause: int
) -> list[Incident]:
    """把一个时间簇按关联维度拆成若干 incident。横向共因优先。

    ⚠️ 这里有一条容易写错、写错了会**改变诊断结论**的规则（第一版就写错了）：

    只凭「同一个 alertname 出现在多个实例上」就判定共因是不成立的。
    实测反例：db01 是「负载 + IO 等待」，web04 是「采集挂了 + 负载 + 内存」，
    两台机器唯一的共同点只是都有 HighLoadAverage。机器之间毫无关联，
    却是两个完全不同的故障。

    第一版这么写之后，db01 的 HighLoadAverage 被拽进假共因组，
    剩下的 HighDiskIOWait 单独成事件 → IO 瓶颈规则（R003）永远命中不了，
    工具给出了「没有规则命中」的结论。**诊断被聚合方式悄悄改变了。**

    所以判定条件是：这些实例除了共有告警之外，**剩下的告警种类必须一致**。
    真正的共因（发布导致三个实例同样挂掉）剩余集合都为空，仍然会被识别。
    """
    by_name: dict[str, list[Alert]] = defaultdict(list)
    for a in cluster:
        by_name[a.alertname].append(a)

    # 每个实例上出现的全部告警名，用于判断「剩余集合是否一致」
    host_names: dict[str, set[str]] = defaultdict(set)
    for a in cluster:
        if a.instance:
            host_names[a.instance].add(a.alertname)

    result: list[Incident] = []
    consumed: set[str] = set()

    for name, members in by_name.items():
        hosts = {m.instance for m in members if m.instance}
        if len(hosts) < min_hosts_for_common_cause:
            continue
        if not _companions_consistent(hosts, name, host_names):
            continue
        result.append(
            Incident(
                incident_id=_incident_id("common_cause", [name] + sorted(hosts)),
                kind="common_cause",
                alerts=list(members),
            )
        )
        consumed.update(m.fingerprint for m in members)

    # 纵向：剩余告警按实例聚
    rest = [a for a in cluster if a.fingerprint not in consumed]
    by_instance: dict[str, list[Alert]] = defaultdict(list)
    for a in rest:
        by_instance[a.instance].append(a)

    for instance, members in by_instance.items():
        if not instance:
            result.append(
                Incident(
                    incident_id=_incident_id(
                        "unscoped", [m.fingerprint for m in members]
                    ),
                    kind="unscoped",
                    alerts=list(members),
                )
            )
            continue
        result.append(
            Incident(
                incident_id=_incident_id("single_host", [instance] + sorted(m.alertname for m in members)),
                kind="single_host",
                alerts=list(members),
            )
        )
    return result


def _companions_consistent(
    hosts: set[str], shared_name: str, host_names: dict[str, set[str]]
) -> bool:
    """共有告警之外，各实例的剩余告警种类是否一致。

    - 全部为空（发布导致三个实例同样挂掉）→ 一致，是真共因
    - 各不相同（db01 有 IO，web04 有采集故障）→ 不一致，不是共因
    - 完全一致但非空（都是「负载 + IO」）→ 一致，更像同一套变更下发
    """
    signatures = {frozenset(host_names[h] - {shared_name}) for h in hosts}
    return len(signatures) == 1
