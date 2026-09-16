# Prometheus 基础与 PromQL

> 实验环境：Ubuntu 22.04 / 2vCPU / 1.6G 内存 / Swap 0
> Prometheus 2.x + node_exporter + Grafana 11

---

## 一、为什么指标监控要用 Prometheus 而不是 ES

| 维度 | Elasticsearch | Prometheus |
|---|---|---|
| 存储单位 | 文档（document） | 时间序列（series） |
| 查询方式 | DSL / Lucene | PromQL |
| 算两个值的差 | 需要 Query A + Query B + Math Expression | 一个减号 |
| 长时间范围 | 越查越慢（扫文档） | 成本差别不大（预聚合） |
| 存储成本 | 高（存全文） | 低（只存 时间戳+浮点数） |
| 擅长 | 日志检索、明细排查 | 指标趋势、告警、容量规划 |

**结论：两者并存才是正确架构。**

- 指标 → Prometheus → 告警
- 明细 → ES → 排查具体是哪一条

一个典型反例：用 ES 做"今日成功数 - 昨日成功数"这类计算，
需要配两个 Query 再加一个 Math Expression；
在 PromQL 里就是一行表达式。

---

## 二、数据模型

### 基本形态

```
metric_name{label1="value1", label2="value2"}  数值
```

实际例子（node_exporter 输出）：

```
node_cpu_seconds_total{cpu="0", mode="idle"}  1234567
node_cpu_seconds_total{cpu="0", mode="user"}  3400
node_cpu_seconds_total{cpu="1", mode="idle"}  1234000
node_cpu_seconds_total{cpu="1", mode="user"}  3200
```

### 核心概念：标签不同 = 不同序列

上面 4 行是**同一个指标名下的 4 条独立时间序列**。
2 核 × 8 种 mode = 16 条序列。

**指标名本质上也是一个标签**：

```promql
node_memory_MemAvailable_bytes
# 完全等价于
{__name__="node_memory_MemAvailable_bytes"}
```

### 基数爆炸（cardinality explosion）

序列数 = 各标签取值数的**乘积**。

| 标签 | 取值数 | 能不能做标签 |
|---|---|---|
| 机构代码 | 400 | 可以（有限、需要分组） |
| 科室 | ~15 | 可以 |
| 状态 success/fail | 2 | 可以 |
| 检查号 exam_id | 每天 10 万+ | **绝对不行** |
| 患者 ID | 无限 | **绝对不行** |
| 错误原文 error_msg | 不可控 | **绝对不行** |
| 错误类型 error_type | ~10（枚举） | 可以 |

**原则：标签是用来"分类"的，不是用来"标识个体"的。**

要查具体是哪一条记录，去 ES / 日志系统查，不要塞进标签。

error_msg 与 error_type 的区别（经典陷阱）：

```
错误：error_msg="Connect to xxx.cn:80 failed: Connection refused (Attempt 4)"
      → 每条都不同（含 IP、重试次数），基数无限

正确：error_type="connection_refused"
      → 归一化成有限枚举值
```

### 健康度自查

```promql
prometheus_tsdb_head_series              # 当前总序列数（单机建议 < 百万级）
count by (job) ({__name__=~".+"})        # 按 job 看序列分布，定位基数爆炸来源
```

---

## 三、四种指标类型

| 类型 | 特征 | 典型例子 | 怎么用 |
|---|---|---|---|
| **Counter** | 只增不减，重启归零 | 请求总数、错误总数 | 必须配 `rate()` |
| **Gauge** | 可增可减 | 内存、温度、队列长度 | 直接看值 |
| **Histogram** | 分桶统计 | 请求耗时 | `histogram_quantile()` |
| **Summary** | 客户端算好分位数 | 同上 | 不能跨实例聚合，少用 |

### Counter 为什么存累计值而不是增量

**为了抗丢点。**

```
存增量：某次抓取失败 → 这段时间的量永久丢失
存累计：某次抓取失败 → 下次抓到的累计值仍然正确，中间只是少一个点
```

`rate()` 还会自动处理 Counter 重启归零的情况（检测到下降就当作重置）。

**Counter 的绝对值没有意义**，它只是一个从进程启动累计到现在的数字。
Grafana 会主动提示：`Selected metric is a counter. Consider calculating rate of counter by adding rate().`

### Histogram 的桶（le）

```
node_request_duration_seconds_bucket{le="0.1"}   耗时 <= 0.1s 的累计个数
node_request_duration_seconds_bucket{le="0.5"}   耗时 <= 0.5s 的累计个数
node_request_duration_seconds_bucket{le="+Inf"}  全部
```

**桶是"累计"的（小于等于），不是"区间"的。**

关键规则：**你要观测的每一个阈值，都必须有对应的桶。**

例如 SLO 是"2 小时内完成"，桶里就必须有 `le="7200"`，
才能直接算出 `bucket{le="7200"} / count` 这个比例。

**桶在埋点时定义，事后改不了历史数据。** 所以设计时就要想清楚。

桶数量也计入基数：`8 个桶 × 400 机构 = 3200 条序列`。
所以 Histogram 的标签要比 Counter 更精简。

### 易混淆：Histogram 的桶 ≠ 时间范围选择器

| | Histogram 的桶（le） | Grafana 时间选择器（近 5m/1h） |
|---|---|---|
| 回答 | **花了多久**（纵向的分布） | **什么时候**（横向的范围） |
| 定义在 | 埋点代码里 | 查看时临时选 |
| 改了会 | 影响后续数据结构 | 只影响当前视图 |

---

## 四、PromQL 的两种核心类型

这是理解 PromQL 的关键分水岭。

### 瞬时向量（Instant Vector）

每条序列在某个时刻的**一个值**。

```promql
node_memory_MemAvailable_bytes
up
node_cpu_seconds_total{mode="idle"}
```

### 区间向量（Range Vector）

每条序列在一段时间内的**一串值**，写法是带 `[5m]`。

```promql
node_cpu_seconds_total[5m]
```

### 关键规则

1. **`rate()` / `increase()` 只能作用于区间向量**
   `rate(node_cpu_seconds_total)` 报错，必须写 `rate(node_cpu_seconds_total[5m])`

2. **对 Gauge 用 `rate()` 没有意义**
   Gauge 会上下波动，算增长率毫无物理含义

3. **区间向量不能直接画图**
   必须经过 `rate()` / `avg_over_time()` 等函数变回瞬时向量

### 时间范围怎么选

`[5m]` 至少要覆盖 **4 个抓取周期**。
scrape_interval=15s 时，`[1m]` 只有 4 个点，抖动大；`[5m]` 有 20 个点，更平滑。

---

## 五、常用表达式

### 选择器

| 写法 | 含义 |
|---|---|
| `{job="node"}` | 等于 |
| `{job!="node"}` | 不等于 |
| `{job=~"node.*"}` | 正则匹配 |
| `{dep!~"US\|超声\|内镜"}` | **正则不匹配（排除法常用）** |

### 内存使用率

```promql
100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)
```

用 MemAvailable 而不是 MemFree —— MemFree 不含可回收的 page cache，会严重高估使用率。
（见 `01-linux/内存管理与OOM排查.md`）

### CPU 使用率

```promql
100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

**用 `100 - idle` 而不是加总 user+sys+iowait**：
mode 的种类随内核版本变化（steal、guest、irq…），加总容易漏；idle 永远存在。

### 磁盘使用率

```promql
100 * (1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
          / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"})
```

必须排除 tmpfs，否则一堆无意义的伪文件系统混进来。

### 分位数

```promql
histogram_quantile(0.99, sum by (le) (rate(xxx_bucket[5m])))
```

注意 `sum by (le)` —— 聚合时**必须保留 le 标签**，否则算不出分位数。

### 目标存活

```promql
up == 0                    # 列出所有采集失败的目标（监控盲区）
count by (job) (up)        # 有哪些 job、各多少实例
```

`up` 是 Prometheus 自动生成的指标，1=抓取成功，0=失败。

---

## 六、systemd 部署要点

```ini
[Service]
User=prometheus                        # 专用用户，最小权限
Type=simple
ExecStart=/opt/prometheus/prometheus \
  --config.file=/opt/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --storage.tsdb.retention.time=15d
ExecReload=/bin/kill -HUP $MAINPID     # 没有这行，systemctl reload 会报错
Restart=on-failure
RestartSec=10s
StartLimitBurst=5                      # 限制重启次数，避免无限重启循环
StartLimitIntervalSec=300
MemoryMax=400M                         # 故障隔离：不让它拖垮整机
OOMScoreAdjust=-100
```

### reload vs restart

| | reload | restart |
|---|---|---|
| 做什么 | 发 SIGHUP，重读配置 | 杀进程再启动 |
| 服务中断 | 不中断 | **中断** |
| 内存状态 | 保留 | 丢失 |
| 前提 | **unit 里必须有 `ExecReload=`** | 无 |

**踩坑记录**：没写 `ExecReload=` 时执行 reload 报
`Job type reload is not applicable for unit prometheus.service`。
原因是 systemd 不知道"重载"该执行什么命令。

**为什么优先用 reload**：
- 监控服务 restart 会中断采集，造成数据点永久缺失
- sshd 必须用 reload —— restart 时如果配置有错，会把自己锁在服务器外面

### 改配置必须先校验

```bash
promtool check config /opt/prometheus/prometheus.yml
promtool check rules /opt/prometheus/rules/*.yml && sudo systemctl reload prometheus
```

`&&` 保证校验不通过就不执行 reload。
养成"校验 → 生效"两步走的习惯，配置类变更几乎不会出事故。

### systemctl edit：不要直接改原文件

```bash
sudo systemctl edit grafana-server        # 只写覆盖片段（推荐）
sudo systemctl edit --full grafana-server # 把整个文件复制到 /etc/ 再编辑
```

打开后会看到一堆 `#` 注释 —— **那是原始配置的只读参考，不是让你取消注释的**。
内容要写在最顶部两行提示之间。

```
/lib/systemd/system/xxx.service                    ← 软件包的，升级会被覆盖
/etc/systemd/system/xxx.service.d/override.conf    ← 你的，升级不动
最终配置 = 原文件 + 覆盖片段
```

这是 Linux 的通用约定：`/lib` 归软件包，`/etc` 归管理员，**`/etc` 优先级更高**。
直接改 `/lib` 下的文件，下次 `apt upgrade` 就全丢了。

验证合并结果：

```bash
systemctl cat grafana-server                    # 看最终生效的完整配置
systemctl show grafana-server | grep -i memorymax
cat /sys/fs/cgroup/system.slice/grafana-server.service/memory.max
```

### 实测结果

```
Memory: 42.8M (max: 400.0M available: 357.1M)
cat memory.max = 419430400          # = 400 × 1024 × 1024，精确生效
```

**配了不等于生效了，必须验证到运行时状态。**

---

## 七、SSH 本地端口转发

### 用途

服务只监听内网 / 不想在安全组开放端口时，用隧道访问。

```bash
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 root@目标IP
```

```
  本机                                目标服务器
┌──────────────┐                 ┌──────────────────┐
│ 浏览器        │                 │                  │
│ localhost:3000│                 │  Grafana:3000    │
│      ↓        │                 │      ↑           │
│ SSH客户端 ────┼─── 加密隧道 ────┼→ SSH服务端       │
│ (监听 3000)   │     (22端口)    │                  │
└──────────────┘                 └──────────────────┘
```

**只有 22 端口在网络上传输，3000/9090 从未暴露。**
服务端看到的请求来源是 localhost，所以即使服务只监听 127.0.0.1 也能访问。

### 更实用的形态：访问第三方内网地址

```bash
# 把内网数据库映射到本地，用本机 SQL 客户端直连
ssh -L 1433:10.115.12.7:1433 用户@跳板机
```

`-L` 第二段可以是**跳板机能访问到的任意地址**，不一定是 localhost。
面试问"没有 VPN 怎么访问内网服务"，答案就是这个。

### 写进配置

```
Host myecs
    HostName 8.141.98.205
    User root
    ServerAliveInterval 30       # 每 30s 心跳，防空闲被踢
    ServerAliveCountMax 6        # 容忍 3 分钟抖动
    LocalForward 3000 localhost:3000    # 注意是空格，不是冒号
    LocalForward 9090 localhost:9090
```

之后 `ssh myecs` 即可，`ssh -f -N myecs` 后台跑不占窗口。

### 两个 localhost 不是同一台机器

| URL | 谁发起 | 在哪解析 | 走隧道 |
|---|---|---|---|
| 浏览器 `localhost:3000` | 本机浏览器 | 本机 | 走 |
| Grafana 数据源 `localhost:9090` | 服务器上的 Grafana | 服务器 | 不走 |

**判断规则：请求在哪台机器上发出，localhost 就是哪台机器。**

数据源不要填公网 IP —— 填 localhost 走 loopback 不出网卡，也不用开安全组。

---

## 八、排查记录：隧道建不起来

现象：服务器上 `curl` 正常，本机浏览器连不上。

```bash
# 服务器侧（全部正常）
ss -lntp | grep -E '3000|9090'    # 两个端口都在 LISTEN
curl -I http://localhost:3000     # 302 Found  → 跳转登录页，正常
curl -I http://localhost:9090     # 405 Method Not Allowed → 不支持 HEAD，正常
```

`302` 和 `405` 都不是错误 —— **只要有 HTTP 响应就说明服务活着**。

```bash
# 本机侧
ssh -G myecs | grep localforward   # 配置正确
lsof -nP -i TCP:3000 -sTCP:LISTEN  # 无输出 ← 问题在这
```

**根因：配置正确但没有活着的 SSH 进程。**

```
~/.ssh/config  = 配置模板（静态的，不会自己发起连接）
ssh myecs      = 应用模板建立连接（隧道随进程存在）
进程结束        = 隧道消失
```

### 教训：配置层 ≠ 运行时层

| 层次 | 验证手段 |
|---|---|
| 配置层 | `ssh -G` / `systemctl cat` / `cat xxx.conf` |
| **运行时层** | `lsof` / `ss -lntp` / `cat memory.max` / `systemctl show` |

**"验了配置层就以为运行时也对"是排障中最常见的误判。**
典型场景：改完配置忘了 reload —— 文件是对的，服务用的还是旧的。

### 另一个坑

```
bind [127.0.0.1]:3000: Address already in use
```

残留的 ssh 进程占着端口。此时 **SSH 能登录成功，但隧道建不起来**，
很容易误判成"服务有问题"。

```bash
pkill -f "ssh.*目标IP"
```

macOS 上如果 `localhost` 打不开，试 `127.0.0.1` —— localhost 可能被解析到 IPv6 `::1`，
而 SSH 默认只在 IPv4 上监听。

---

## 九、Grafana Explore 使用要点

### Builder vs Code

| | Builder | Code |
|---|---|---|
| 方式 | 下拉框拼装 | 直接写 PromQL |
| 数学运算 | 很别扭 | 随便写 |
| 适合 | 探索有哪些指标 | **实际工作** |

Builder 的 `Select metric` 下拉框是个好东西 —— 列出所有指标名并可搜索，
用来探索"这个系统有什么指标"。写查询则一律用 Code。

### 多个 Query 的坑

同时启用多个 Query 会**全部叠加在同一个图**里。

实测踩坑：同时放了「内存使用率(0-100)」和「CPU 累计秒数(百万级)」，
Y 轴被撑到 600 万，两条百分比曲线被压成贴地直线，完全看不出变化。

**量纲不同的指标不要画在一个图里。**

- 眼睛图标 = 临时禁用该 Query（保留配置）
- 垃圾桶 = 删除
- **Split 按钮** = 左右分屏，各自独立查询但**时间轴联动** ← 排障时对比两个指标的最佳方式

### Graph vs Table

带多个标签的原始指标（如 `node_cpu_seconds_total`）要切 **Table** 看，
能直接看到每一行的标签组合，这是理解"标签即序列"最直观的方式。

---

## 十、探索一个陌生 Prometheus 的方法

接手陌生环境时的固定动作：

### 从 exporter 侧看

```bash
curl -s localhost:9100/metrics | head -50                        # 看原始形态
curl -s localhost:9100/metrics | grep "^# TYPE" | wc -l          # 多少个指标
curl -s localhost:9100/metrics | grep "^# TYPE" | awk '{print $4}' | sort | uniq -c   # 按类型统计
curl -s localhost:9100/metrics | grep -v "^#" | wc -l            # 多少条序列
```

`# HELP` 是说明，`# TYPE` 是类型 —— 这两行注释是 exporter 自描述的关键。

### 从 Prometheus API 侧看

```bash
curl -s localhost:9090/api/v1/label/__name__/values | python3 -m json.tool   # 所有指标名
curl -s localhost:9090/api/v1/labels | python3 -m json.tool                  # 所有标签名
curl -s localhost:9090/api/v1/label/mode/values | python3 -m json.tool       # 某标签的取值
```

### 从 Web UI 看（Grafana 看不到这些）

```
Status → Targets          哪些目标在采、哪些是 DOWN ← 最有价值的一页
Status → Configuration    完整的 prometheus.yml
Alerts                    规则的 Inactive/Pending/Firing 状态
Graph                     跑 PromQL
```

### 优先级最高的三条查询

```promql
up == 0                    # 当前的监控盲区
count by (job) (up)        # 监控覆盖了哪些系统
prometheus_tsdb_head_series # 规模与健康度
```

---

## 十一、把业务指标接入 Prometheus 的设计方法

### 顺序：从"要回答什么问题"出发，不是从"我有什么数据"出发

这是指标设计最容易搞反的地方。

```
① 今天采了多少数据？        → 计数
② 失败率多少？              → 成功/失败分开计数
③ 从产生到上传花了多久？     → 耗时分布
④ 两类数据对得上吗？        → 两个独立计数
⑤ 哪个机构/科室有问题？     → 分组维度
⑥ 队列积压了吗？           → 当前值
```

### 类型判断规则

```
问「累计发生了多少次」  → Counter
问「现在是多少」        → Gauge
问「花了多久、分布如何」→ Histogram
```

### 标签判断规则

对每个候选标签问三个问题：

1. 取值有多少个？会不会无限增长？
2. 我会用它来分组查询吗？
3. 不要它我会损失什么？

然后**算乘积**，这是唯一的硬约束。

### 参考设计（数据采集类服务）

```
指标名                    类型        标签                          用途
──────────────────────────────────────────────────────────────────────
xxx_records_total        Counter    org, dep, data_type, status   完整性 SLI 的分子
xxx_source_records_total Counter    org, dep                      完整性 SLI 的分母
xxx_lag_seconds          Histogram  org                           及时性 SLI
                         桶: 300,900,1800,3600,7200,14400,86400,+Inf
xxx_errors_total         Counter    org, error_type               失败原因分布
xxx_pending_queue        Gauge      org                           积压量
api_requests_total       Counter    api_name, status              接口可用性 SLI
```

基数估算：`400 org × 15 dep × 2 status × 2 type = 24000` 条 —— 完全可接受。

### 最关键的一条：分母必须独立于被测系统

`xxx_source_records_total`（源头应有多少条）**不能由采集程序自己上报处理结果**，
必须由独立探针去查源头系统。

否则系统故障时分子分母同步变小，SLI 反而更好看 —— 指标会在最需要它的时候失灵。
（详见 `SRE理念-SLI与SLO设计.md`）

同理，`api_requests_total` 必须同时统计成功与失败。
只统计成功数是系统性观测缺陷 —— 分母不存在，可用率永远算不出来。

### 用 PromQL 反向验证设计

设计完必须验证：**这些指标能不能算出我要的 SLI？** 算不出来说明设计有缺口。

```promql
# 完整性
sum by (org) (xxx_records_total{status="success"}) / sum by (org) (xxx_source_records_total)

# 及时性（2 小时内占比）—— 这就是为什么桶里必须有 7200
sum(rate(xxx_lag_seconds_bucket{le="7200"}[1h])) / sum(rate(xxx_lag_seconds_count[1h]))

# P99
histogram_quantile(0.99, sum by (le) (rate(xxx_lag_seconds_bucket[5m])))

# 接口可用性
sum(rate(api_requests_total{status="success"}[5m])) / sum(rate(api_requests_total[5m]))

# 失败原因 Top5
topk(5, sum by (error_type) (rate(xxx_errors_total[1h])))
```

---

## 十二、速查

```bash
# 服务
systemctl cat / show / status <svc>
promtool check config|rules <file>
systemctl reload <svc>          # 需要 ExecReload

# 探索
curl -s localhost:9100/metrics | grep "^# TYPE"
curl -s localhost:9090/api/v1/label/__name__/values

# 隧道
ssh -f -N myecs
lsof -nP -i TCP:3000 -sTCP:LISTEN
```

```promql
up == 0
count by (job) (up)
prometheus_tsdb_head_series
100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)
100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
histogram_quantile(0.99, sum by (le) (rate(xxx_bucket[5m])))
topk(5, sum by (error_type) (rate(xxx_errors_total[1h])))
```
