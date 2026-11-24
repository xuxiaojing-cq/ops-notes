# alert-insight：告警聚合与 AI 归因

把 Alertmanager 的告警从「一堆通知」变成「少量事件 + 根因候选 + 下一步命令」。

> 完整记录与实测数据见 [../告警聚合与AI归因.md](../告警聚合与AI归因.md)
>
> **状态：已验证。** 71 个单元测试通过；CLI 端到端、webhook 接收器、
> 降级路径、真实 LLM 调用均已实测。开发中修掉 3 个真实 bug（假共因、
> 接收器不认数组、无状态导致抖动检测失效），细节见笔记。

## 要解决什么

一次故障往往产生几十上百条告警。人面对的是刷屏的通知列表，要自己在脑子里做三件事：
哪些是同一回事、谁是因谁是果、先查什么。这三件事可以自动化。

```text
Alertmanager webhook
   ↓ normalize   payload 拆成扁平的单条告警，不做任何推断
   ↓ aggregate   按实例 / 告警名 / 时间窗聚成「事件」，几十条 → 几个
   ↓ diagnose    确定性规则给出根因候选 + 证据 + 判别方法 + 验证命令
   ↓ llm         可选增强：只能在给定候选里排序和表达，输出结构化 JSON 并校验
   ↓ render      Markdown 事件卡片 / JSON，退出码可被脚本接住
```

## 三个设计决定

### 1. 不把原始告警直接丢给大模型

直觉做法是把告警 JSON 发给 LLM 问「什么原因」。生产上这样做会死：

| 问题 | 后果 |
|---|---|
| 告警风暴一次几百条 | token 爆炸、延迟不可控 |
| LLM 超时 / 限流 / 网关异常 | 告警通道跟着挂，这是 P0 |
| 模型编造根因 | 比没有诊断更危险——它会把人带向错误方向 |

所以先用确定性代码降维，LLM 只处理少量结构化事实。

### 2. 规则层是底座，LLM 是增强

`diagnose.py` 的规则**必然有输出**，不依赖任何外部服务。LLM 挂掉时自动降级，
并且**降级原因会打印在输出里**——绝不静默降级，否则人会误以为模型认可了规则结论。

### 3. 幻觉用工程手段约束，不靠「提示词写得好」

三道闸：

1. **输入闭集**：prompt 里只有 payload 事实 + 规则候选，明确禁止引入候选之外的根因
2. **输出结构化**：要求返回固定字段的 JSON（自由文本无法校验）
3. **输出校验**：解析失败、引用了不存在的 `rule_id`、缺字段 —— 一律判失败并降级

第 3 条里「引用不存在的 rule_id」是最关键的检测点：模型一旦开始编造根因，
必然要造一个 ID，而 ID 不在白名单里就会被拦下。

## 归因规则

规则按「先判断告警可不可信，再判断哪里坏了」的顺序组织：

| 规则 | 判断 | 为什么重要 |
|---|---|---|
| R001 | 采集侧故障 | exporter 挂了会让同机所有指标告警，看着像整机崩溃，业务可能完全正常。**先问数据可不可信** |
| R002 | 横向共因 | 多实例同时同症状 → 根因在公共部分（发布/依赖/网络），**逐台排查是纯浪费时间** |
| R003 | 负载 + IO 等待 | 进程卡在 D 状态等 IO，负载计入但 CPU 不忙 |
| R004 | 只有负载异常 | 可能是进程堆积、容器限流，或阈值本身贴着常态水位 |
| R005 | 内存压力 | 区分泄漏（RSS 单调上升）与容量不足（稳定高位） |
| R006 | 磁盘空间 | 空间 / inode / 已删除但被持有，三种情况处置不同 |
| R007 | 单实例服务不可用 | 先分「进程没了」还是「进程在但不响应」 |
| R008 | 告警抖动 | 会被 resolved 掩盖，必须单独拎出来 |
| R009 | payload 被截断 | 影响面被低估，不能基于截断数据判断 |
| R010 | 证书到期 | 多实例同时到期通常是续期机制坏了 |
| R011 | 错误率升高 | 灰度期间整体错误率会被权重稀释 |

规则知识来自本仓库已记录的真实排查经验（02-monitoring 的告警设计与 inhibit 实验、
03-container 的负载异常排查、05-cicd 的三种发布策略实测），不是通用文档的清单。

### 置信度为什么只有三档

规则是启发式。写 `confidence: 0.87` 是假精确——那个数字没有统计来源。
所以只分 high / medium / low，并且**每条规则必须给出判别方法**：
真正降低不确定性的是去跑验证命令，不是模型给的分数。

每条规则都带 `counter_evidence`（如果不是这个原因，你会看到什么）。
排查的核心动作是排除法，只给正面假设会把人锁死在一个方向上。

## 用法

无第三方依赖，Python 3.10+。

```bash
cd 07-ai/alert-insight

# 分析 fixture（不调模型）
python3 -m alert_insight.cli --no-llm fixtures/synthetic-storm.json

# 调模型增强（需要 ANTHROPIC_API_KEY）
python3 -m alert_insight.cli fixtures/synthetic-storm.json

# 给脚本用的 JSON 输出
python3 -m alert_insight.cli --no-llm --format json fixtures/synthetic-storm.json

# 从 stdin 读
cat payload.json | python3 -m alert_insight.cli --no-llm -
```

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 无告警 / 仅 info |
| 1 | 最高 warning |
| 2 | 最高 critical |
| 3 | 输入或参数错误 |

输入错误用独立的 3，不和严重度混在一起——否则脚本分不清「critical 告警」和「文件没找到」。

### 接真实 Alertmanager

```bash
# 启接收器，原始 payload 落盘用于后续回放
python3 -m alert_insight.server --port 5001 --dump-dir ./captured
```

```yaml
# alertmanager.yml
receivers:
  - name: 'insight'
    webhook_configs:
      - url: 'http://127.0.0.1:5001/alert'
        send_resolved: true    # 默认 false，不开就检测不到抖动
```

⚠️ `captured/` 里是真实 payload，含主机名、实例地址、业务指标，**已在 .gitignore 中排除，不要提交**。

## 验证步骤

```bash
cd 07-ai/alert-insight

# 1. 单元测试（实测 71 passed）
python3 -m unittest discover -s tests -v

# 2. 端到端，不调模型（实测 6 个事件，退出码 2）
python3 -m alert_insight.cli --no-llm fixtures/synthetic-storm.json; echo "退出码: $?"

# 3. 降级行为
env -u ANTHROPIC_API_KEY python3 -m alert_insight.cli fixtures/synthetic-storm.json | head -40

# 4. 真实模型调用
python3 -m alert_insight.cli fixtures/synthetic-common-cause.json

# 5. webhook 接收器
python3 -m alert_insight.server --port 5001 --dump-dir ./captured
curl -sS -X POST -H 'content-type: application/json' \
  --data-binary @fixtures/synthetic-common-cause.json http://127.0.0.1:5001/alert
```

### 环境变量

| 变量 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | 模型调用；缺省时自动降级到规则层 |
| `ANTHROPIC_BASE_URL` | 网关地址 |
| `AI_MODEL` | 覆盖默认模型名（默认值会随供应商下线而失效，实测撞过一次） |
| `AI_CA_BUNDLE` / `SSL_CERT_FILE` | 显式指定 CA 包（内网自签证书用；macOS 的 Python 不自动读系统证书库） |

⚠️ 不会为了跑通而关闭 TLS 校验。

## 测试覆盖的重点

测试不是为了覆盖率，是盯住几条容易悄悄出错的行为：

- **抖动会不会被去重抹掉** — 先按 fingerprint 合并就永远检测不到状态翻转，
  所以 cli 刻意不用 `normalize_many`。测试里有一条专门对比两种做法的结果。
- **采集侧告警会不会被误判成业务故障** — 这是方向性错误，比没有结论更糟
- **LLM 编造 rule_id 时校验拦不拦得住**
- **LLM 不可用时会不会静默降级** — 必须留下原因
- **规则给了结论是否都给了验证方法** — 遍历所有规则断言

## 已知局限

| 局限 | 说明 |
|---|---|
| 规则是启发式 | 不是因果推断，结论需要靠验证命令确认 |
| 只看 payload | 不查 Prometheus 历史曲线，无法判断趋势 |
| 不感知变更 | 判断「是不是刚发布导致的」仍需人工关联，这是最该补的下一步 |
| 时间窗固定 | 没有按告警类型自适应 |
| 无状态 | 不记忆历史事件，无法识别「这个问题上周也出过」 |
| 未接通知渠道 | 只输出到 stdout，没推到 IM |
| LLM 成本未测 | 每个事件一次调用，告警风暴时的费用需要实测后加限流 |

## 下一步

1. 跑通验证，用真实输出写笔记
2. 接变更记录（发布时间线），让 R002 能直接指出「与 10:02 的发布吻合」
3. 加事件去重与记忆，识别重复发生的问题
4. 接 IM 通知，带上归因结果而不只是原始告警
