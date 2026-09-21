# Alertmanager 告警路由与通知

> 实验环境：Ubuntu 22.04 / 2vCPU / 1.6G 内存
> Prometheus 2.53.0 + node_exporter + Alertmanager 0.27.0
> 本文所有时间戳均为实验实测数据

---

## 一、职责边界：告警不是 Prometheus 一个人的事

最常见的误解是「告警是 Prometheus 发的」。实际是**两个独立进程分工**：

| | Prometheus | Alertmanager |
|---|---|---|
| 干什么 | 按 `expr` 周期求值，满 `for` 时长后标记为 `firing` | 接收 firing 告警，决定**发给谁、怎么合并、要不要发** |
| 不管什么 | 不管发给谁 | 不管告警怎么产生 |
| 状态机 | `inactive → pending → firing` | `分组 → 抑制 → 静默 → 路由 → 通知` |
| 有无状态 | **无状态**，每个评估周期整批重发 | **有状态**，维护告警组的计时器 |

```
node_exporter ──scrape──> Prometheus ──evaluate──> firing
                                          │
                                   POST /api/v2/alerts（每个 evaluation_interval 重发）
                                          ▼
                                   Alertmanager
                                   ├─ route tree 路由匹配
                                   ├─ group_by   分组
                                   ├─ inhibit    抑制
                                   ├─ silence    静默
                                   └─> receiver  通知
```

### ⭐ 为什么要拆成两个进程

Prometheus 的发送是**无状态、幂等**的——每个评估周期把当前所有 firing 整批 POST，不关心发没发过。

这个设计的动机是**支持多副本**：生产上 Prometheus 通常跑两台做冗余，两台都往同一个 AM 推，靠 AM 按 `group_by` 去重收敛成一条通知。如果去重逻辑放在 Prometheus 侧，两个副本就得互相通信同步状态，架构会复杂得多。

> 面试问「Prometheus 每 15s 重发一次，为什么值班的人不会每 15s 被轰炸」——答案是**去重全在 AM 侧**，并能说出多副本这个动机，档次就上去了。

### pending：降噪的第一道闸

`expr` 已为真但未满 `for` 时长的中间态。CPU 尖刺 10 秒就回落的，压根到不了 firing。

问「怎么避免告警抖动」，第一条答案就是 `for`。

---

## 二、四个时间参数

`route` 里四个参数名字像，管的事完全不同。

```yaml
route:
  group_by: ['alertname', 'instance']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: 'default'
```

| 参数 | 管什么 | 设错的后果 |
|---|---|---|
| `group_by` | 按哪些 label **合并成一条通知** | 设 `[...]` 全部打散 = 一台机器挂了收 5 条；设太粗 = 不同故障混一条看不清 |
| `group_wait` | 新组**首次通知前**攒批等多久 | 设 0 = 第一条立刻发，后续的各发各的，白分组 |
| `group_interval` | 组内**有变化**时，最快多久发下一条 | 设太小 = 故障扩散期被刷屏 |
| `repeat_interval` | 组内**没变化**时，多久重复提醒 | 设太小 = 告警疲劳；设太大 = 忘了还没处理 |

### ⚠️ 最易混的两个

- `group_interval` → 组内容**变了**才触发
- `repeat_interval` → 组内容**没变**的周期性唠叨

### ⭐ group_interval 是「发车时刻表」不是「延迟」

实测数据说明问题（抑制实验前的一轮）：

| 事件 | 时间 |
|---|---|
| 告警 `startsAt` | 22:09:38 |
| webhook 收到 firing | **22:09:48** |
| 告警 `endsAt`（恢复） | 22:24:53 |
| webhook 收到 resolved | **22:29:48** |

firing 侧 38 → 48 正好 10 秒 = 该子路由的 `group_wait: 10s`。

resolved 侧却隔了 **4 分 55 秒**。因为 AM 对每个组维护一个按 `group_interval: 5m` 走的节拍器，从首次通知起打点：

```
22:09:48 ← 首次通知
22:14:48   （无变化，不发）
22:19:48   （无变化，不发）
22:24:48   （无变化，不发）  ← 告警 22:24:53 才恢复，晚 5 秒，错过这班车
22:29:48 ← 赶上下一班，发 resolved
```

**就差 5 秒，恢复通知晚了 5 分钟。**

结论：`group_interval` 不是「延迟 N 分钟」，是「**每 N 分钟才有一次发车机会**」，错过等下一班。设太大，故障扩散和恢复消息都会滞后。

### 生产经验值

```yaml
group_wait: 30s
group_interval: 5m
repeat_interval: 4h    # ⚠️ 别小于 1h
```

`repeat_interval` 太小 → 值班的人一晚上被同一条叫醒 20 次 → 最后一定是把告警静音 → **告警疲劳**。这在 SRE 里比漏报更常见的失效模式：不是没告警，是告警太多导致真故障被淹没。

---

## 三、四种降噪手段的层次

| 手段 | 配置者 | 时机 | 解决什么 |
|---|---|---|---|
| `for` | 写规则的人 | 规则里静态定义 | 瞬时抖动 |
| `group_*` | 配 AM 的人 | 配置里静态定义 | 同一件事发几条 |
| `inhibit_rules` | 配 AM 的人 | 配置里静态定义，**运行时动态评估** | 两件事有因果，只报因不报果 |
| `silence` | **值班的人临时点** | 运行时动态创建 | 计划内变更、已知问题处理中 |

前三个是「配好就不动」，silence 是唯一**人主动操作**的。

---

## 四、抑制（inhibit）

分组解决「同一件事发几条」，抑制解决「**两件事有因果关系**」。

机器宕了 → 该报「机器没了」，不该同时报「它磁盘满了」「它内存高了」——后者是前者的**症状**不是独立故障。

```yaml
inhibit_rules:
  - source_matchers:
      - severity = "critical"
    target_matchers:
      - severity = "warning"
    equal: ["instance"]
```

读法：**当同一个 `instance` 上有 critical 在 firing 时，压掉该 instance 的所有 warning。**

### ⭐ equal 是灵魂

没有 `equal`，就变成「全局任何一条 critical 压掉全世界的 warning」——A 机器宕机会把 B 机器的磁盘告警一起吃掉，**真故障被静默，比没配抑制更危险**。

> 面试问抑制能主动说出 `equal` 作用的人不多。

### 实验验证

实验规则（与 `NodeExporterDown` 同 `expr`、同 `instance`，但 severity 为 warning）：

```yaml
- alert: NodeExporterFlaky
  expr: up{job="node"} == 0
  for: 1m
  labels:
    severity: warning
  annotations:
    summary: "node_exporter 采集异常（实验用 warning）"
```

停掉 node_exporter 后：

```bash
# Prometheus 侧：两条都 firing
$ curl -s localhost:9090/api/v1/alerts | ...
firing NodeExporterDown  critical
firing NodeExporterFlaky warning

# AM 侧默认视图：只有 critical
$ amtool alert
Alertname         Starts At                Summary            State
NodeExporterDown  2026-09-21 12:25:38 UTC  node_exporter 已停止  active

# 加 --inhibited 才看得到 warning
$ amtool alert --inhibited
Alertname          Starts At                Summary                            State
NodeExporterFlaky  2026-09-21 12:25:38 UTC  node_exporter 采集异常（实验用 warning）  suppressed
```

⭐ **`suppressed` 这个状态是关键**：warning 确实到了 AM、AM 也认它、它就在那儿，**只是被标记为不通知**。和「规则没触发」是完全不同的两回事。

### ⭐ A/B 对照才算证明

只看到「warning 没发」不能排除是别的原因。把 `inhibit_rules` 注释掉后 reload：

```
20:25:48  <<critical>>  firing  NodeExporterDown      ← 抑制生效期
20:29:54  <<warning>>   firing  NodeExporterFlaky     ← 注释掉 inhibit_rules 后立刻放行
```

同一条告警、同一个 instance、同样在 firing，**唯一变量是抑制规则在不在**。

### ⚠️ 抑制是动态评估的

一旦抑制条件消失（配置改了、或 source 告警恢复），被压的 target 告警会**立刻补发**。

所以故障恢复后有时会收到一堆「迟到」的 warning——不是系统抽风，是抑制解除后积压的告警放行了。

---

## 五、静默（silence）

唯一由人临时操作的降噪手段。

```bash
# 配置默认地址，省得每次敲 --alertmanager.url
mkdir -p ~/.config/amtool
cat > ~/.config/amtool/config.yml <<'EOF'
alertmanager.url: http://localhost:9093
EOF

# 打静默
amtool silence add alertname=NodeExporterDown \
  --duration=10m \
  --comment="计划内维护：批量部署停服务"

# 查
amtool silence query
amtool alert --silenced     # 加这个才看得到被静默的

# 提前撤销
amtool silence expire <silence-id>
```

### ⚠️ 静默期间连 resolved 也不发

静默是把**这一组的通知整个掐掉**，firing 和 resolved 都不发。

**实操陷阱**：变更窗口打了静默，期间真出了故障，故障恢复的消息也收不到。所以**时长要卡紧**，别图省事打 24 小时。

### expire ≠ delete

`expire` 是**提前置为过期**，历史记录保留。能查到「谁、什么时候、静默了什么、为什么」——这是审计要求，也是 `--comment` 必填的原因。

### ⭐ 和批量部署交付的结合

停服务窗口不静默 → 告警群炸 → 炸多了同事把群设免打扰 → **真故障也被忽略**。告警疲劳就是这么来的。

规范动作：

```
变更前：amtool silence add（带工单号、预计时长）
  ↓
变更中：执行
  ↓
变更后：amtool silence expire（主动撤销，不等自然过期）
  ↓
观察：确认告警恢复正常
```

---

## 六、路由树与 receiver

```yaml
route:
  group_by: ["alertname", "instance"]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: "default-webhook"

  routes:
    - matchers:
        - severity = "critical"
      receiver: "critical-webhook"
      group_wait: 10s          # 覆盖父级的 30s
      repeat_interval: 1h      # 覆盖父级的 4h

receivers:
  - name: "default-webhook"
    webhook_configs:
      - url: "http://127.0.0.1:5001/warning"
        send_resolved: true
  - name: "critical-webhook"
    webhook_configs:
      - url: "http://127.0.0.1:5001/critical"
        send_resolved: true
```

### 路由匹配规则

- **深度优先**，第一个匹配的子路由胜出
- 子路由**继承**父路由的参数，显式写的覆盖继承值
- 默认匹配到就停止；加 `continue: true` 才会继续往后匹配（一条告警发多个渠道时用）

实验验证：`NodeExporterDown`（critical）走 `<<critical>>`，`NodeExporterFlaky`（warning）走 `<<warning>>`，同一秒 resolved 但分两个 receiver 发出——路由分流生效。

### ⚠️ send_resolved 默认是 false

很多团队的告警只有「炸了」没有「好了」，值班的人不知道该不该收手，得手动去看面板。**恢复通知一定要开。**

---

## 七、webhook payload 结构

学习阶段用 webhook + 自写接收器比配邮件划算：不用任何账号密码，而且**能看到 AM 实际发出的 JSON 结构**——这是写告警模板的全部素材。

实测 payload（resolved 那条）：

```json
{
  "receiver": "critical-webhook",
  "status": "resolved",
  "alerts": [
    {
      "status": "resolved",
      "labels": {
        "alertname": "NodeExporterDown",
        "instance": "localhost:9100",
        "job": "node",
        "severity": "critical"
      },
      "annotations": { "summary": "node_exporter 已停止" },
      "startsAt": "2026-09-20T14:09:38.18Z",
      "endsAt": "2026-09-20T14:24:53.18Z",
      "generatorURL": "http://HOST:9090/graph?g0.expr=up%7Bjob%3D%22node%22%7D+%3D%3D+0&g0.tab=1",
      "fingerprint": "0c3f1299754f298a"
    }
  ],
  "groupLabels":  { "alertname": "NodeExporterDown", "instance": "localhost:9100" },
  "commonLabels": { "alertname": "NodeExporterDown", "instance": "localhost:9100",
                    "job": "node", "severity": "critical" },
  "commonAnnotations": { "summary": "node_exporter 已停止" },
  "externalURL": "http://HOST:9093",
  "version": "4",
  "groupKey": "{}/{severity=\"critical\"}:{alertname=\"NodeExporterDown\", instance=\"localhost:9100\"}",
  "truncatedAlerts": 0
}
```

### ⭐ groupKey：路由链路写在脸上

```
{}/{severity="critical"}:{alertname="NodeExporterDown", instance="localhost:9100"}
└┬┘ └────────┬────────┘ └──────────────────┬──────────────────────────────────┘
根route    子路由matchers                  group_by 的实际取值
matchers
```

排查「告警为什么没发到某个群」时，`groupKey` **直接告诉你它最终落在哪条 route 上**，比翻配置猜快得多。

### 关键字段

| 字段 | 用途 |
|---|---|
| `status` | `firing` / `resolved` |
| `groupLabels` | 恒等于 `group_by` 那几个 label |
| `commonLabels` | 组内所有告警**实际共有**的 label。多机器告警时 `instance` 不同，就不会出现在这里 |
| `alerts[].labels` | 单条告警全部 label |
| `alerts[].annotations` | 规则里写的 `summary` / `description` |
| `startsAt` / `endsAt` | firing 时 `endsAt` 是零值 `0001-01-01T00:00:00Z` |
| `fingerprint` | **label set 的哈希**，AM 靠它识别「是不是同一条告警」 |
| `generatorURL` | 回跳 Prometheus，**自动带上触发它的 PromQL** |
| `truncatedAlerts` | 组内告警过多时被截断的数量，非 0 说明有告警没发出去 |
| `version` | payload schema 版本，写解析代码要判 |

### ⚠️ fingerprint 的推论

label 改一个字，fingerprint 就变，AM 当成新告警处理。

**所以告警规则里绝对不要放会变的 label**（比如带时间戳、带当前值的 label）——每次评估都生成新 fingerprint，去重完全失效，等于每个周期发一条。

### 写模板的取舍

- 想拿「这组的共同特征」→ `commonLabels` / `commonAnnotations`
- 想拿单条细节 → 遍历 `alerts[]`

企微/钉钉那种排版好看的卡片，本质就是拿这堆字段套 Go template。「告警消息里带上机构名」这类需求 = 在规则的 `annotations` 里用 `{{ $labels.xxx }}` 把维度带进来，AM 侧再渲染。

---

## 八、部署要点

### systemd unit

```ini
[Unit]
Description=Prometheus Alertmanager
Wants=network-online.target
After=network-online.target

[Service]
User=alertmanager
Group=alertmanager
Type=simple
ExecStart=/opt/alertmanager/alertmanager \
  --config.file=/opt/alertmanager/alertmanager.yml \
  --storage.path=/var/lib/alertmanager \
  --web.listen-address=0.0.0.0:9093
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5s
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```

### ⚠️ reload 不是 restart

AM 和 Prometheus 都支持 `SIGHUP` 热加载。

**restart 会丢掉内存里的 silence 状态和分组计时器**——正在生效的静默没了，变更窗口里告警会炸出来。生产上改配置一律 reload。

### 改配置必须先校验

```bash
amtool check-config /opt/alertmanager/alertmanager.yml
promtool check config  /opt/prometheus/prometheus.yml
promtool check rules   /opt/prometheus/rules/basic.yml
```

### Prometheus 侧必须配 alerting 段

这段缺了，AM 装了也是摆设——**Prometheus 根本不知道它存在**：

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['localhost:9093']
```

验证：

```bash
curl -s localhost:9090/api/v1/alertmanagers | python3 -m json.tool
```

`activeAlertmanagers` 里要有 `http://localhost:9093/api/v2/alerts`，`droppedAlertmanagers` 要为空。

---

## 九、⭐ 排查：为什么没收到告警

三种「没收到」原因完全不同，必须分清：

| 现象 | 原因 | 怎么查 |
|---|---|---|
| Prometheus 里就没有 | 规则没触发 / 还在 pending | `curl localhost:9090/api/v1/alerts` 看 `state` |
| Prometheus 有，AM 没有 | 链路断了 | `curl localhost:9090/api/v1/alertmanagers` 看 `dropped` |
| AM 有但没通知 | 被抑制 / 被静默 / 在攒批窗口里 | `amtool alert --inhibited` / `--silenced` |

对应的状态词：

- Prometheus 侧：`inactive` / `pending` / `firing`
- AM 侧：`active` / `suppressed`（被抑制或被静默）

---

## 十、JSON Lines 格式

webhook 接收器按 **JSON Lines（ndjson）** 落盘——每行一个独立 JSON 对象，**整个文件不是合法 JSON**。

```bash
tail -2 xxx.log | python3 -m json.tool
# Extra data: line 2 column 1    ← 解析器在第 2 行发现「JSON 已结束但还有内容」

tail -1 xxx.log | python3 -m json.tool   # ✅ 只取一行
tail -2 xxx.log | jq .                   # ✅ jq 天然支持逐行流式
```

⭐ 日志领域普遍用这个格式（Filebeat、Loki、Docker json-file 驱动）。原因：**追加写不用改文件结构**。如果整个文件是 JSON 数组，每次追加都得回去改末尾的 `]`，并发写直接崩；而且 ndjson 可以流式逐行处理，不用把整个文件读进内存。

> `python3 -m json.tool` 默认 ASCII 转义，中文显示成 `已停止` 不是乱码，加 `--no-ensure-ascii` 即可。

---

## 十一、速查

```bash
# 服务
systemctl reload alertmanager          # ⚠️ 不要 restart
systemctl reload prometheus

# 校验
amtool check-config /opt/alertmanager/alertmanager.yml
promtool check config /opt/prometheus/prometheus.yml
promtool check rules  /opt/prometheus/rules/*.yml

# 链路
curl -s localhost:9090/api/v1/alertmanagers | python3 -m json.tool
curl -s localhost:9090/api/v1/alerts        | python3 -m json.tool

# 告警查询
amtool alert                  # 默认只显示 active
amtool alert --inhibited      # 被抑制的
amtool alert --silenced       # 被静默的

# 静默
amtool silence add alertname=XXX --duration=10m --comment="工单号+原因"
amtool silence query
amtool silence expire <id>

# Web UI（SSH 转发 9093）
# Alerts / Silences / Status(看生效的完整配置) / Help
```

---

## 十二、面试可讲点

**1. 为什么 Prometheus 和 Alertmanager 要拆开**

> Prometheus 的发送是无状态、幂等的，每个评估周期把当前所有 firing 整批 POST，不关心发没发过。去重、分组、抑制全在 AM 侧，它按 `group_by` 维护告警组状态机。这么设计是为了支持 Prometheus 多副本——两台同时往一个 AM 推，靠 AM 收敛成一条通知，副本之间不用互相同步状态。

**2. 怎么治理告警疲劳**（分层答，别只说一个）

> 四个层次：规则层用 `for` 过滤瞬时抖动；分组层用 `group_by` 把同一故障的多条合并；抑制层用 `inhibit_rules` 做因果压制，只报根因不报症状，关键是要带 `equal` 限定同一维度；运维层用 silence 处理计划内变更。另外 `repeat_interval` 不能小于 1 小时，否则值班的人会被同一条反复叫醒，最后的结果一定是把告警静音——那比漏报更危险。

**3. 变更窗口怎么处理告警**

> 规范做法是变更前用 silence 按 label 精确静默，comment 带上工单号和预计时长，变更完成后主动 expire 而不是等自然过期。silence 的历史记录可审计，能追溯哪些告警是变更导致的。⚠️ 要注意静默期间 resolved 也不发，所以时长要卡紧。

**4. 告警没收到怎么排查**

> 分三段定位：Prometheus 里有没有（规则没触发还是在 pending）、有没有推到 AM（看 `/api/v1/alertmanagers` 的 dropped）、AM 收到了为什么不发（`amtool alert --inhibited/--silenced`）。AM 侧被压的告警状态是 `suppressed`，它是存在的、只是不通知，这和规则没触发是两回事。

---

## 附：实验环境清单

| 组件 | 位置 | 端口 |
|---|---|---|
| Prometheus | `/opt/prometheus` | 9090 |
| node_exporter | — | 9100 |
| Grafana | — | 3000 |
| Alertmanager | `/opt/alertmanager` | 9093 |
| webhook 接收器 | `/opt/webhook/receiver.py` | 5001 |
| 告警规则 | `/opt/prometheus/rules/basic.yml` | — |
| webhook 日志 | `/var/log/am-webhook.log` | — |
