"""规则归因层：给事件产出候选根因、判别方法和处置建议。

为什么要有这一层（而不是直接让 LLM 归因）
--------------------------------------
三个理由，按重要程度排：

1. **必然有输出。** LLM 会超时、会限流、会被网关拦。告警通道不能依赖一个
   可能挂掉的外部服务，所以确定性规则是底座，LLM 只是增强。
2. **约束幻觉。** LLM 拿到的不是「自由发挥题」，而是「从这些候选里选、
   用这些证据说明」。输入边界明确，编造空间就小。
3. **可审计。** 规则命中的原因能逐条解释，出错能改；模型给的结论没法复查。

置信度为什么用三档而不是百分数
---------------------------
这些规则本质是启发式。写 `confidence: 0.87` 是假精确——那个数字没有任何
统计来源。所以只分 high / medium / low，并且**每条规则必须给出判别方法**：
真正降低不确定性的是「去跑这条命令看结果」，不是模型给的分数。

反事实（counter_evidence）
-----------------------
每条规则都要说明「如果不是这个原因，你会看到什么」。排查的核心动作是排除法，
只给正面假设会把人往一个方向带偏。这一栏也是 LLM 的锚点：让它必须在
已给出的判别条件内讨论，不许另起炉灶。

知识来源
-------
规则内容来自本仓库已记录的真实排查经验（02-monitoring 的告警设计、
03-container 的 Compose 负载异常排查、05-cicd 的三种发布策略实测），
不是从通用文档抄的清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .aggregate import Incident
from .normalize import Alert

# ---------------------------------------------------------------------------
# 症状分类：对告警命名不敏感
# ---------------------------------------------------------------------------
# 真实环境里告警名五花八门（HighLoad / NodeLoadHigh / load_too_high），
# 规则如果硬编码 alertname 就只能在自己的环境里用。所以先把告警归到症状类别。

# ⚠️ 关键词必须写成「已去掉下划线和连字符」的形式——classify() 会先把 haystack
# 里的 _ 和 - 剥掉再匹配，关键词里留下划线就永远匹配不上（写这版时踩过）。
SYMPTOM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "scrape_down": ("exporterdown", "targetdown", "scrape", "upzero", "nodeexporter"),
    "service_down": ("servicedown", "instancedown", "probefail", "healthcheck", "httpdown", "down"),
    "load": ("load",),
    "cpu": ("cpu", "throttle"),
    "memory": ("mem", "oom", "swap"),
    "disk_space": ("diskspace", "filesystem", "diskfull", "inode"),
    "disk_io": ("iowait", "diskio", "ioutil", "iops", "readonly"),
    "network": ("network", "packetloss", "conntrack", "tcp", "latency"),
    "cert": ("cert", "tls", "ssl", "expiry", "expir"),
    "error_rate": ("errorrate", "5xx", "http5", "errors"),
    "restart": ("restart", "crashloop", "flap"),
    "truncated": ("alertmanagertruncated",),
}


def classify(alert: Alert) -> set[str]:
    """把一条告警归到零个或多个症状类别。

    用 alertname + summary 一起判断：有些团队告警名很简略，信息都在 summary 里。
    一条告警可能命中多个类别（比如 DiskReadOnly 既是 disk_io 也可能触发 service_down），
    这是刻意的——宁可多命中让规则层去筛，也不要漏。
    """
    haystack = (alert.alertname + " " + alert.summary).lower().replace("_", "").replace("-", "")
    found: set[str] = set()
    for symptom, keywords in SYMPTOM_KEYWORDS.items():
        if any(k in haystack for k in keywords):
            found.add(symptom)
    # scrape_down 的关键词和 service_down 有重叠（都含 down），
    # 采集侧更具体，命中后就不要再算作业务侧，否则归因方向会反。
    if "scrape_down" in found:
        found.discard("service_down")
    return found


def incident_symptoms(incident: Incident) -> set[str]:
    out: set[str] = set()
    for a in incident.alerts:
        out |= classify(a)
    return out


# ---------------------------------------------------------------------------
# 诊断结果
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """一条归因结论。"""

    rule_id: str
    hypothesis: str  # 根因假设
    confidence: str  # high | medium | low
    evidence: list[str] = field(default_factory=list)  # 支持它的事实（来自 payload）
    counter_evidence: list[str] = field(default_factory=list)  # 如果不是它，会看到什么
    verify: list[str] = field(default_factory=list)  # 判别命令
    actions: list[str] = field(default_factory=list)  # 处置建议
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "hypothesis": self.hypothesis,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "counter_evidence": self.counter_evidence,
            "verify": self.verify,
            "actions": self.actions,
            "note": self.note,
        }


CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1}

Rule = Callable[[Incident, set[str]], Finding | None]
_RULES: list[tuple[str, Rule]] = []


def rule(rule_id: str):
    def deco(fn: Rule):
        _RULES.append((rule_id, fn))
        return fn

    return deco


# ---------------------------------------------------------------------------
# 规则：最高优先级是「这个告警可信吗」
# ---------------------------------------------------------------------------


@rule("R001-scrape-side-failure")
def _scrape_side(incident: Incident, symptoms: set[str]) -> Finding | None:
    """采集侧故障：指标断了，不等于业务挂了。

    这条规则必须排在最前面。exporter 挂掉时，依赖该 exporter 的所有指标
    都会因为「没有数据」触发告警，看起来像整机崩溃，实际业务可能完全正常。
    先判断告警本身可不可信，再谈根因。
    """
    if "scrape_down" not in symptoms:
        return None

    # 证据要分开写：哪些是采集侧故障本身，哪些是「依赖它、因而不可信」的告警。
    # 第一版把两者混成一句「事件内含采集侧告警：A, B」，会让人误以为 B 也是采集侧故障。
    scrape_names = sorted(a.alertname for a in incident.alerts if "scrape_down" in classify(a))
    dependent_names = sorted(
        {a.alertname for a in incident.alerts if "scrape_down" not in classify(a)}
    )
    other = sorted(symptoms - {"scrape_down", "truncated"})
    conf = "high" if other else "medium"
    ev = [f"采集侧故障：{', '.join(scrape_names)}"]
    if dependent_names:
        ev.append(f"依赖该采集的指标告警（数据可能已中断）：{', '.join(dependent_names)}")

    return Finding(
        rule_id="R001-scrape-side-failure",
        hypothesis="exporter / 抓取目标不可达，指标中断。同机器的其他指标告警很可能是数据缺失导致的假告警，不是业务故障",
        confidence=conf,
        evidence=ev,
        counter_evidence=[
            "如果业务真的也挂了：从外部直接访问业务端口应该失败",
            "如果只是采集挂了：业务端口正常响应，只有 exporter 端口连不上",
        ],
        verify=[
            "curl -sS -o /dev/null -w '%{http_code}\\n' http://<实例>:<业务端口>/healthz   # 业务侧是否真的挂了",
            "curl -sS -o /dev/null -w '%{http_code}\\n' http://<实例>:9100/metrics        # 采集侧",
            "systemctl status node_exporter",
            "ss -lntp | grep -E '9100|<业务端口>'",
        ],
        actions=[
            "先确认业务是否受影响，再决定是否按故障处理",
            "恢复 exporter；若频繁掉线查它自身的内存限制与 OOM 记录",
            "告警规则上给依赖该采集的指标加 `up{job=...} == 1` 前置条件，或配 inhibit 规则让采集侧告警抑制业务侧告警（本仓库 02-monitoring 已验证 inhibit 的 equal 用法）",
        ],
        note="判断顺序：先问「数据可信吗」，再问「哪里坏了」。",
    )


@rule("R002-common-cause")
def _common_cause(incident: Incident, symptoms: set[str]) -> Finding | None:
    """横向共因：多实例同时同症状。

    对批量交付场景，这是最值钱的一条判断。多个实例在同一时间窗出现同一告警，
    几乎不可能是各自独立坏掉——「逐台排查」在这里是纯浪费时间。
    """
    if incident.kind != "common_cause":
        return None

    n = len(incident.instances)
    spread = incident.spread_seconds

    # 置信度取决于两件事：摊开的实例数，以及触发时间是否真的挤在一起。
    # 真共因（一次发布打挂三台）触发时间通常在几十秒内；
    # 跨度上百秒说明它们只是「恰好落在同一个聚合窗口」，共因的把握要往下调。
    if spread is None:
        conf = "medium"
    elif spread <= 60 and n >= 3:
        conf = "high"
    elif spread <= 60:
        conf = "medium"
    elif spread <= 180:
        conf = "medium" if n >= 3 else "low"
    else:
        conf = "low"

    ev = [
        f"受影响实例 {n} 个：{', '.join(incident.instances[:8])}"
        + ("…" if n > 8 else ""),
        f"相同告警：{', '.join(incident.alertnames)}",
        f"首次触发时间：{incident.started_at.isoformat() if incident.started_at else '未知'}",
    ]
    if spread is not None:
        ev.append(f"组内首次触发时间跨度：{spread:.0f} 秒")

    return Finding(
        rule_id="R002-common-cause",
        hypothesis=f"共因故障：{n} 个实例在同一时间窗出现相同告警，根因在它们的公共部分（最近一次发布 / 共享依赖 / 网络 / 配置下发 / 证书），不在单台机器上",
        confidence=conf,
        evidence=ev,
        counter_evidence=[
            "如果是各自独立的问题：触发时间会分散，而不是挤在同一个窗口里",
            "如果是共因：未受影响的实例应该有某个共同特征不同（版本、所在网段、配置版本）",
        ],
        verify=[
            "比对受影响与未受影响实例的版本号 / 配置版本 / 所在网段",
            "查同时间窗内的变更记录：最近一次发布、配置下发、证书轮换、依赖服务升级",
            "在任一受影响实例上验证公共依赖：DNS 解析、到数据库/网关的连通性",
        ],
        actions=[
            "优先查变更，不要逐台排查",
            "若时间点与发布吻合，按发布回滚流程处理（本仓库 05-cicd 已有自动回滚与蓝绿/金丝雀切流实测）",
            "确认影响面：统计受影响实例占总量的比例，决定是否需要对外通告",
        ],
        note="横向关联的价值在于**改变排查方向**，而不只是把告警数量合并。",
    )


@rule("R003-load-io-bound")
def _load_io(incident: Incident, symptoms: set[str]) -> Finding | None:
    """高负载 + IO 等待：IO 瓶颈，不是 CPU 不够。"""
    if "load" not in symptoms or "disk_io" not in symptoms:
        return None
    return Finding(
        rule_id="R003-load-io-bound",
        hypothesis="IO 瓶颈导致负载升高：进程卡在不可中断睡眠（D 状态）等 IO，负载计入但 CPU 并不忙。常见诱因是后台任务（备份、日志归档、系统自动更新）抢占磁盘",
        confidence="high" if "cpu" not in symptoms else "medium",
        evidence=[
            f"同时出现负载与 IO 类告警：{', '.join(sorted(symptoms & {'load', 'disk_io', 'cpu'}))}",
            "未同时出现 CPU 饱和告警" if "cpu" not in symptoms else "同时存在 CPU 告警，需区分是 IO 等待还是真实 CPU 争用",
        ],
        counter_evidence=[
            "如果是 CPU 不够：%us + %sy 高、%wa 低、R 状态进程多",
            "如果是 IO 瓶颈：%wa 高、D 状态进程多、util 接近 100%",
        ],
        verify=[
            "uptime; vmstat 1 5          # 看 r / b 两列，b 是等 IO 的进程数",
            "top -bn1 | head -5          # 重点看 %wa",
            "ps -eo stat,pid,comm | awk '$1 ~ /D/'   # D 状态进程是 IO 卡住的直接证据",
            "iostat -x 1 3               # %util 与 await",
            "iotop -bon2 2>/dev/null | head -20",
        ],
        actions=[
            "定位占用 IO 的进程，确认是业务还是系统后台任务",
            "系统自动更新类任务（unattended-upgrade / 包管理器后台任务）应改到低峰期或关闭自动触发",
            "负载高但 %wa 高时不要盲目加 CPU 配额，先解决 IO",
        ],
        note="负载（load average）统计的是「可运行 + 不可中断」进程数，不是 CPU 使用率。这是最常被误读的指标。",
    )


@rule("R004-load-without-cpu-io")
def _load_only(incident: Incident, symptoms: set[str]) -> Finding | None:
    """只有负载告警，没有 CPU / IO / 内存告警。"""
    if "load" not in symptoms:
        return None
    if symptoms & {"disk_io", "cpu", "memory"}:
        return None
    return Finding(
        rule_id="R004-load-without-cpu-io",
        hypothesis="负载升高但三大资源指标都没告警：可能是进程数堆积（D 状态或大量短命进程）、容器 CPU 配额被限流、或负载阈值本身设得贴近常态水位",
        confidence="medium",
        evidence=[
            "事件内只有负载类告警，无 CPU / IO / 内存类告警",
            f"涉及实例：{', '.join(incident.instances) or '未标注'}",
        ],
        counter_evidence=[
            "如果是阈值问题：历史曲线上负载长期在阈值附近波动，而不是突然抬升",
            "如果是真实问题：负载曲线有明确的阶跃点，且能找到对应的进程变化",
        ],
        verify=[
            "uptime                       # 1/5/15 分钟三个值，看是在上升还是回落",
            "vmstat 1 5                   # r 与 b 分别有多少",
            "ps -eo stat,pid,ppid,comm --sort=-stat | head -20",
            "cat /sys/fs/cgroup/cpu.stat 2>/dev/null | grep throttled   # 容器是否被限流",
        ],
        actions=[
            "先看 1/5/15 分钟趋势判断是瞬时尖峰还是持续上升，瞬时尖峰通常不需要处置",
            "若为阈值过敏：按实际水位调整阈值或延长 `for`，而不是让人去习惯忽略告警",
        ],
        note="「长期在告警边缘」比「不告警」更糟——它会训练人忽略整个告警通道。",
    )


@rule("R005-memory-pressure")
def _memory(incident: Incident, symptoms: set[str]) -> Finding | None:
    if "memory" not in symptoms:
        return None
    with_restart = "restart" in symptoms or "service_down" in symptoms
    return Finding(
        rule_id="R005-memory-pressure",
        hypothesis="内存压力"
        + ("，且已造成进程被杀或服务不可用（OOM Killer 介入的典型组合）" if with_restart else "，尚未看到进程被杀"),
        confidence="high" if with_restart else "medium",
        evidence=[
            # 只列告警名，不按条数重复展开——同一 fingerprint 的 firing/resolved
            # 序列会产生一堆同名项，列出来只是噪音
            f"内存类告警：{', '.join(sorted({a.alertname for a in incident.alerts if 'memory' in classify(a)}))}",
        ]
        + (["同时存在服务不可用 / 重启类告警"] if with_restart else []),
        counter_evidence=[
            "如果是 OOM：dmesg 里有 Killed process 记录，容器 State.OOMKilled 为 true",
            "如果只是缓存占用高：available 仍然充足，无 OOM 记录（Linux 的 used 包含缓存，不能直接当作内存不足）",
        ],
        verify=[
            "free -m                      # 看 available，不是 used",
            "dmesg -T | grep -i -E 'killed process|out of memory' | tail -20",
            "docker inspect <容器> --format '{{.State.OOMKilled}} {{.RestartCount}}'",
            "ps aux --sort=-rss | head -10",
        ],
        actions=[
            "确认是泄漏（RSS 持续单调上升）还是容量不足（稳定高位）——处置方式完全不同",
            "容器场景给 --memory 限制，避免单个容器拖垮整机",
            "低内存机器上注意发布策略的内存开销（蓝绿需要两份常驻，本仓库 05-cicd 有实测数据）",
        ],
    )


@rule("R006-disk-space")
def _disk(incident: Incident, symptoms: set[str]) -> Finding | None:
    if "disk_space" not in symptoms:
        return None
    return Finding(
        rule_id="R006-disk-space",
        hypothesis="磁盘空间或 inode 不足。常见元凶：容器日志未限制大小、应用日志未轮转、备份文件堆积、已删除但仍被进程持有的文件",
        confidence="high",
        evidence=[f"磁盘空间类告警：{', '.join(incident.alertnames)}"],
        counter_evidence=[
            "如果是普通文件堆积：du 的统计和 df 基本吻合",
            "如果是「已删除但被持有」：df 显示满，du 却找不到对应大小的文件，需要 lsof 查",
            "如果是 inode 耗尽：df -h 显示还有空间，但写入照样失败",
        ],
        verify=[
            "df -h; df -i                 # 空间和 inode 都要看",
            "du -x -h --max-depth=1 / 2>/dev/null | sort -h | tail -15",
            "lsof +L1 2>/dev/null | head -20            # 已删除仍被持有的文件",
            "docker system df              # 镜像/容器/卷各占多少",
        ],
        actions=[
            "清理前先确认增长源，否则清完还会满",
            "容器日志必须限制：--log-opt max-size / max-file（本仓库 05-cicd 的 playbook 已固定带上）",
            "磁盘类告警阈值要留够处置时间，按「剩余天数」而不是单纯百分比更有意义",
        ],
    )


@rule("R007-service-down-single")
def _service_down(incident: Incident, symptoms: set[str]) -> Finding | None:
    if "service_down" not in symptoms:
        return None
    if incident.kind == "common_cause":
        return None  # 交给 R002 处理，避免结论重复
    return Finding(
        rule_id="R007-service-down-single",
        hypothesis="单实例服务不可用。按概率排序：进程已退出（配置错误 / 缺必需环境变量 / 端口冲突）> 被 OOM 杀掉 > 健康检查探针本身的问题",
        confidence="medium",
        evidence=[
            f"实例：{', '.join(incident.instances) or '未标注'}",
            f"告警：{', '.join(incident.alertnames)}",
            "同事件内未发现其他实例有相同告警（非共因）",
        ],
        counter_evidence=[
            "如果进程真的退出了：ss 里端口无人监听，容器 Exited",
            "如果进程在但不响应：端口有监听，但请求超时或返回错误——方向完全不同，要查应用内部（锁、线程池、下游依赖）",
            "如果探针有问题：手工 curl 正常，只有探针判失败",
        ],
        verify=[
            "docker ps -a --filter name=<容器>           # Up 还是 Exited，退出码是多少",
            "docker logs --tail 50 <容器>",
            "ss -lntp | grep <端口>                      # 端口上到底有没有进程",
            "curl -sS -o /dev/null -w '%{http_code} %{time_total}\\n' http://127.0.0.1:<端口>/healthz",
        ],
        actions=[
            "先区分「进程没了」还是「进程在但不响应」，这是两条不同的排查路径",
            "容器反复重启时看退出码和 RestartCount，不要只看最后一次日志",
            "若与发布时间吻合，优先回滚而不是现场调试",
        ],
        note="容器启动成功 ≠ 服务可用。本仓库 05-cicd 实测过：docker run 返回后应用还要若干秒才绑定端口，这段时间端口上没人监听。",
    )


@rule("R008-flapping")
def _flapping(incident: Incident, symptoms: set[str]) -> Finding | None:
    if incident.kind != "flapping":
        return None
    flips = max(incident.flap_counts.values(), default=0)
    return Finding(
        rule_id="R008-flapping",
        hypothesis=f"告警抖动（{flips} 次状态翻转）：指标在阈值附近反复穿越。要么是资源处于临界水位，要么是阈值/`for` 配置过敏",
        confidence="high",
        evidence=[
            f"同一 fingerprint 在观察期内翻转 {flips} 次",
            f"告警：{', '.join(incident.alertnames)}",
        ],
        counter_evidence=[
            "如果是真实临界：指标曲线确实在阈值上下来回，扩容/优化后抖动消失",
            "如果是配置过敏：指标常态就在阈值附近，`for` 设得太短让瞬时尖峰也发车",
        ],
        verify=[
            "在 Prometheus 里看该指标最近 6 小时曲线，确认是否长期贴着阈值",
            "检查规则的 `for`：过短会让瞬时尖峰触发告警",
        ],
        actions=[
            "抖动必须单独处理，不能当普通告警——resolved 会把它掩盖掉",
            "调整阈值到实际水位之上，或延长 `for` 过滤瞬时尖峰",
            "短期可用 silence 压制，但要带到期时间，避免长期静默掩盖真实故障",
        ],
        note="抖动的危害不在于单次误报，而在于它让人逐渐不再相信告警。",
    )


@rule("R009-truncated")
def _truncated(incident: Incident, symptoms: set[str]) -> Finding | None:
    if "truncated" not in symptoms:
        return None
    return Finding(
        rule_id="R009-truncated",
        hypothesis="Alertmanager 截断了部分告警，当前看到的影响面被低估",
        confidence="high",
        evidence=[a.summary for a in incident.alerts if "truncated" in classify(a)],
        counter_evidence=["若未截断，payload 的 truncatedAlerts 应为 0"],
        verify=[
            "打开 Alertmanager UI 查看该组的完整告警列表",
            "检查 webhook 接收端是否有 payload 大小限制",
        ],
        actions=[
            "不要基于被截断的 payload 判断影响面",
            "按 group_by 维度拆细分组，减少单组告警数量",
        ],
    )


@rule("R010-cert-expiry")
def _cert(incident: Incident, symptoms: set[str]) -> Finding | None:
    if "cert" not in symptoms:
        return None
    return Finding(
        rule_id="R010-cert-expiry",
        hypothesis="证书即将过期或已过期。多实例同时出现时通常是同一张证书或同一套自动续期机制失效",
        confidence="high",
        evidence=[f"证书类告警：{', '.join(incident.alertnames)}", f"涉及 {len(incident.instances)} 个实例"],
        counter_evidence=[
            "如果是单张证书到期：只有使用它的实例告警",
            "如果是续期机制坏了：所有依赖自动续期的实例会陆续告警",
        ],
        verify=[
            "echo | openssl s_client -connect <域名>:443 -servername <域名> 2>/dev/null | openssl x509 -noout -dates",
            "检查自动续期任务的最近执行记录与退出码",
        ],
        actions=[
            "证书类告警的提前量要够长（建议 30 天以上），这类问题处置需要走流程",
            "续期后确认服务已重新加载证书——nginx 需要 reload，否则仍用旧证书",
        ],
        note="与 nginx 的 upstream 主机名缓存同理：配置/证书更新后不 reload，进程用的还是旧的。",
    )


@rule("R011-error-rate")
def _error_rate(incident: Incident, symptoms: set[str]) -> Finding | None:
    if "error_rate" not in symptoms:
        return None
    return Finding(
        rule_id="R011-error-rate",
        hypothesis="错误率升高。若正处于灰度期间，要注意整体错误率会被灰度权重稀释，不能直接用整体指标判断新版本好坏",
        confidence="medium",
        evidence=[f"错误率类告警：{', '.join(incident.alertnames)}", f"涉及 {len(incident.instances)} 个实例"],
        counter_evidence=[
            "如果是新版本问题：错误集中在灰度实例上，按实例拆分后差异明显",
            "如果是下游依赖问题：所有实例错误率同步上升，与版本无关",
        ],
        verify=[
            "按实例拆分错误率，而不是只看总量",
            "检查网关访问日志里的 upstream 地址分布，确认错误来自哪些后端",
            "确认当前是否有灰度在进行中，灰度权重是多少",
        ],
        actions=[
            "灰度期间的判定要按灰度实例自身算错误率；只能拿到整体指标时，阈值要除以灰度权重",
            "注意网关可能把连接层失败静默重试到其他后端，客户端看到 200，问题被掩盖——不能只看状态码，要做业务级验证",
        ],
        note="本仓库 05-cicd 的金丝雀实测验证过稀释效应：金丝雀 100% 不可用时，池子整体只表现出约 10% 异常（等于权重）。",
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def diagnose(incident: Incident) -> list[Finding]:
    """对单个事件跑全部规则，按置信度排序。

    刻意允许多条规则同时命中：真实故障常常是多个因素叠加，
    只给一个「最可能原因」会把人锁死在一个方向上。
    """
    symptoms = incident_symptoms(incident)
    findings: list[Finding] = []
    for _rule_id, fn in _RULES:
        try:
            result = fn(incident, symptoms)
        except Exception as exc:  # pragma: no cover - 单条规则出错不能影响整体
            findings.append(
                Finding(
                    rule_id=_rule_id,
                    hypothesis=f"规则执行异常，已跳过：{type(exc).__name__}",
                    confidence="low",
                    note="这是归因引擎自身的缺陷，不是业务故障",
                )
            )
            continue
        if result is not None:
            findings.append(result)

    if not findings:
        findings.append(_unknown(incident, symptoms))

    findings.sort(key=lambda f: CONFIDENCE_ORDER.get(f.confidence, 0), reverse=True)
    return findings


def _unknown(incident: Incident, symptoms: set[str]) -> Finding:
    """没有规则命中时，诚实地说不知道，并给通用排查入口。

    这比硬凑一个结论重要：假的归因会把排查带偏，代价高于没有归因。
    """
    return Finding(
        rule_id="R000-no-rule-matched",
        hypothesis="没有规则命中，无法给出根因假设",
        confidence="low",
        evidence=[
            f"告警：{', '.join(incident.alertnames)}",
            f"识别到的症状类别：{', '.join(sorted(symptoms)) or '无'}",
        ],
        counter_evidence=[],
        verify=[
            "按 USE 方法逐层排查：使用率 / 饱和度 / 错误，分别看 CPU、内存、磁盘、网络",
            "查该时间窗内的变更记录",
        ],
        actions=[
            "若这类告警会反复出现，给归因引擎补一条规则（规则库的价值来自持续积累）",
        ],
        note="症状类别为空通常说明告警命名没被识别，可在 SYMPTOM_KEYWORDS 里补关键词。",
    )
