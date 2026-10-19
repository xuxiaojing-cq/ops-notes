# Docker Compose 编排监控栈

> 实验环境：Ubuntu 22.04 / Docker Engine 29.8.1 / Docker Compose V2（`docker compose`）
> 编排栈：Prometheus + Alertmanager + node_exporter + Grafana + webhook
> 本文所有关键时间戳、IP、体积均为实机执行结果，非示意。

---

## 一、Compose 是什么，解决什么问题

单机上一个容器搞不定完整服务。监控栈至少四个组件，还有启动顺序、网络互通、数据持久化、配置注入的问题。纯 `docker run` 要写四条长命令，还要手工 `docker network create`，改一个端口就得重敲。

Compose 的做法：**把「一组容器的期望状态」写成一个 YAML，由它负责创建网络、按依赖启动、注入配置。**

```bash
docker compose up -d     # 期望状态 → 实际状态
docker compose down      # 销毁
```

核心心智模型：**声明式**。你描述"要什么"，不是"怎么做"。

### 项目名（project name）

Compose 用**目录名**作为项目名，所有资源都带这个前缀：

```
/root/day9/monitoring/  →  project = monitoring
```

产生的资源：

| 资源类型 | 实际名称 |
|---|---|
| 网络 | `monitoring_default` |
| 卷 | `monitoring_prom-data`、`monitoring_grafana-data` |
| 容器 | `day9-prometheus` 等（因为显式写了 `container_name`） |

注意最后一行：**写了 `container_name` 就不加项目前缀**，这是本文实验栈的做法。不写的话容器名会是 `monitoring-prometheus-1`。

⚠️ 项目名冲突：两个不同目录但同名（比如两个 `monitoring/`）会互相覆盖网络和卷。用 `-p` 显式指定：

```bash
docker compose -p day10 up -d
```

---

## 二、网络：自动创建 + 服务名即 DNS

### 自动创建的默认网络

`docker compose up` 会自动建一个 bridge 网络，实测配置：

```
Name=monitoring_default
Driver=bridge
Subnet=172.18.0.0/16
Gateway=172.18.0.1
```

容器 IP 分配（实测）：

```
day9-webhook        172.18.0.2
day9-prometheus     172.18.0.3
day9-alertmanager   172.18.0.4
day9-node-exporter  172.18.0.5
day9-grafana        172.18.0.6
```

**这些 IP 不可依赖**——容器重建后会变。真正稳定的是**服务名**。

### 服务名即 DNS

同一个网络内，容器可以用**服务名**互相访问，不用管 IP：

```yaml
# prometheus/prometheus.yml
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["node-exporter:9100"]     # ← 服务名，不是 IP
```

```yaml
# alertmanager/alertmanager.yml
receivers:
  - name: webhook
    webhook_configs:
      - url: "http://webhook:5001/alert"    # ← 服务名
```

实测抓取目标全部 `up`：

```
job=prometheus    http://localhost:9090/metrics            up
job=alertmanager  http://alertmanager:9093/metrics         up
job=node          http://node-exporter:9100/metrics        up
```

⭐ **面试答法**：「Compose 每条 compose.yml 自动建一个项目专属 bridge 网络，服务名注册进 Docker 内嵌 DNS，同网络容器直接用服务名解析。所以配置里写 `prometheus:9090` 而不是 IP——IP 会随容器重建变化，服务名不会。」

### 内嵌 DNS 是 127.0.0.11

容器内看 `/etc/resolv.conf`（实测）：

```
nameserver 127.0.0.11
options timeout:2 attempts:3 rotate single-request-reopen ndots:0

# Based on host file: '/etc/resolv.conf' (internal resolver)
# ExtServers: [host(127.0.0.53)]
```

两个关键点：

1. **`127.0.0.11` 是 Docker 在容器 netns 里监听的一个虚拟 DNS**，不是宿主机上的进程。
2. **外部域名靠转发**：`ExtServers: [host(127.0.0.53)]` -- Docker 把宿主机的 DNS 配置（这里是 systemd-resolved 的 127.0.0.53）作为上游，容器查外网域名时由它转发。

验证转发能力（实测）：

```bash
docker exec day9-prometheus wget -T5 https://github.com -O /dev/null
# 返回 0，外部域名解析正常
```

`ndots:0` 也值得注意：它让带点的名字优先按绝对域名解析，减少无效的 search 域名拼接尝试。

⭐ **和默认 bridge 的区别**：`docker run` 不加 `--network` 时用的是默认 `bridge`，**那个网络没有内嵌 DNS，容器间不能用名字互访**，只能 `--link`（已废弃）或走 IP。Compose 自动建的网络是自定义 bridge，才有 DNS 能力。

---

## 三、端口映射：只有宿主机侧需要改

这台机器上已有 systemd 服务占了 9090 / 9093 / 9100 / 3000 / 5001。Compose 里的做法是**改宿主机侧端口，容器侧不动**：

```yaml
ports:
  - "127.0.0.1:19090:9090"   # 宿主机 19090 → 容器 9090
  - "127.0.0.1:19093:9093"
  - "127.0.0.1:19100:9100"
  - "127.0.0.1:13000:3000"
```

实测映射：

```
day9-prometheus      9090/tcp -> 127.0.0.1:19090
day9-alertmanager    9093/tcp -> 127.0.0.1:19093
day9-node-exporter   9100/tcp -> 127.0.0.1:19100
day9-grafana         3000/tcp -> 127.0.0.1:13000
day9-webhook         （无输出，未映射）
```

**为什么容器侧不用改**：每个容器有自己的 net namespace，容器内的 9090 之间根本不冲突。冲突只发生在**宿主机**这一侧。

**为什么绑 `127.0.0.1`**：默认 `0.0.0.0` 会监听所有网卡，ECS 公网可直连。学习用途绑回环最安全。

`docker compose config` 展开后能看到完整形态：

```yaml
ports:
  - mode: ingress
    host_ip: 127.0.0.1
    target: 9090
    published: "19090"
    protocol: tcp
```

### webhook 不映射端口

```yaml
webhook:
  build:
    context: .
    dockerfile: Dockerfile.webhook
  container_name: day9-webhook
  restart: unless-stopped
  # 没有 ports
```

它只被 Alertmanager 通过服务名访问（`http://webhook:5001/alert`），属于**内部服务**，不需要暴露到宿主机。`docker port day9-webhook` 返回空，验证了这一点。

⭐ **反复踩过的坑**：宿主机上执行 `curl localhost:5001` 也返回了 OK，容易误以为端口映射生效了。实际是宿主机上**另一个 systemd 服务** `am-webhook` 正监听 `127.0.0.1:5001`。排查方法：

```bash
ss -lntp | grep 5001        # 看实际是哪个进程在监听
docker port day9-webhook    # 空 → 容器没映射
```

---

## 四、depends_on 与 healthcheck

### 只有 depends_on：只保证启动顺序

实测配置（A）：

```yaml
services:
  slow:
    command: ["sh","-c","echo SLOW_START; sleep 12; echo SLOW_READY; sleep infinity"]
  fast:
    depends_on: [slow]
    command: ["sh","-c","echo FAST_START; sleep infinity"]
```

实测日志时间戳：

```
slow: 12:52:40.567  SLOW_START
fast: 12:52:40.804  FAST_START     ← 只比 slow 晚 0.24 秒
slow: 12:52:52.569  SLOW_READY     ← slow 真正可用是在 12 秒后
```

**结论：`fast` 在 `slow` 还没就绪的 12 秒前就起来了。** `depends_on` 只保证"容器已创建/已启动"，不保证"服务能响应"。启动瞬间连过去，大概率 connection refused。

### depends_on + condition: service_healthy

实测配置（B）：

```yaml
services:
  slow:
    command: ["sh","-c","echo SLOW_START; sleep 12; touch /tmp/ready; echo SLOW_READY; sleep infinity"]
    healthcheck:
      test: ["CMD","test","-f","/tmp/ready"]
      interval: 2s
      timeout: 2s
      retries: 30
      start_period: 1s
  fast:
    depends_on:
      slow:
        condition: service_healthy
    command: ["sh","-c","echo FAST_START; sleep infinity"]
```

实测时间戳：

```
slow: 12:53:21.054  SLOW_START
slow: 12:53:33.057  SLOW_READY
fast: 12:53:34.939  FAST_START     ← 比 SLOW_READY 晚 1.88 秒
```

**结论：`fast` 被阻塞到 `slow` 健康检查通过后才启动。** 1.88 秒的间隔 = 下一次健康检查轮询（`interval: 2s`）的等待时间。

### 对比总结

| 配置 | 依赖方启动时刻 | 是否等服务就绪 |
|---|---|---|
| `depends_on: [slow]` | 依赖容器创建后约 0.24s | ❌ 否 |
| `depends_on: {slow: {condition: service_healthy}}` | 依赖健康检查通过后约 1.88s | ✅ 是 |

### 本栈的实际用法

```yaml
grafana:
  depends_on:
    prometheus:
      condition: service_healthy
```

```yaml
prometheus:
  healthcheck:
    test: ["CMD", "wget", "-q", "--spider", "http://localhost:9090/-/healthy"]
    interval: 5s
    timeout: 3s
    retries: 5
    start_period: 10s
```

`start_period: 10s` 让 Prometheus 启动阶段健康检查失败不计入 `retries`，避免刚开始就被判死。

### 健康检查命令必须存在

早期实验用 `curl` 写健康检查，实测 `ExitCode: -1`：

```
ExitCode=-1
```

**`-1` 的含义是 OCI exec 失败，即命令在 `$PATH` 里找不到**；非 0（如 1、127）才是命令跑起来但失败。`prom/prometheus:latest` 基于 busybox，只有 `wget` 没有 `curl`。

写健康检查前先验证：

```bash
docker run --rm --entrypoint sh prom/prometheus:latest -c "command -v wget; command -v curl"
```

现在这套跑起来是正常的：

```
ExitCode=0 Output=    （连续多次 ExitCode=0）
```

### ⚠️ unhealthy 不会触发重启

`healthcheck` 只改变状态标记，**不触发任何动作**。这和 Kubernetes 的 `livenessProbe` 完全不同——K8s 探针失败会重启容器，Docker 的 healthcheck 失败只是把状态改成 `unhealthy`。

实测：让一个服务健康检查持续失败，观察 5 分钟，`Restarts=0`，容器一直挂着。

⭐ **面试答法**：「Docker 的 healthcheck 是纯状态检查，失败只会让 `docker ps` 显示 `unhealthy`，配合 `depends_on: condition: service_healthy` 用来控制启动顺序。要自动恢复得靠 restart 策略或外面的编排工具。K8s 的 livenessProbe 才会主动重启容器。」

---

## 五、卷与配置管理

### 命名卷 vs bind mount

```yaml
volumes:
  - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro   # bind mount
  - prom-data:/prometheus                                            # 命名卷
```

| | bind mount | 命名卷 |
|---|---|---|
| 宿主机路径 | 你指定（如 `./prometheus/`）| Docker 管理（`/var/lib/docker/volumes/`）|
| 适合 | 配置文件、代码 | 运行时数据 |
| 备份 | 直接拷文件 | `docker run --volumes-from` 或拷 `_data` |

本栈实测：

```
monitoring_prom-data    -> /var/lib/docker/volumes/monitoring_prom-data/_data
monitoring_grafana-data -> /var/lib/docker/volumes/monitoring_grafana-data/_data

76M   monitoring_prom-data/_data
53M   monitoring_grafana-data/_data
```

### `:ro` 的意义

配置文件全部加了 `:ro`：

```yaml
- ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
- ./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
- ./grafana/provisioning:/etc/grafana/provisioning:ro
```

两个作用：

1. **防止程序意外改写**。写挂载会直接报 `Read-only file system`，而不是静默损坏配置。
2. **宣示意图**：这是配置来源，不是数据。

⚠️ **数据卷不能加 `:ro`**。Prometheus 要写 `/prometheus` 里的 TSDB，Grafana 要写 `/var/lib/grafana` 里的 SQLite。加了 `:ro` 启动即报错。

### down 不删卷

实测：

```bash
$ docker volume ls | grep monitoring
local     monitoring_grafana-data
local     monitoring_prom-data

$ docker compose down
 ✔ Container day9-grafana       Removed    13.7s
 ✔ Container day9-webhook       Removed    13.7s
 ✔ Container day9-alertmanager  Removed    13.7s
 ✔ Container day9-node-exporter Removed    13.7s
 ✔ Container day9-prometheus    Removed     1.0s
 ✔ Network monitoring_default   Removed     0.2s

$ docker volume ls | grep monitoring
local     monitoring_grafana-data
local     monitoring_prom-data          # ← 仍然存在
```

重新 `up -d` 后数据恢复：

```bash
$ docker compose up -d && sleep 15
 ✔ Container day9-prometheus  Healthy  6.7s
 ✔ Container day9-grafana     Started  6.8s

$ curl -s 'localhost:19090/api/v1/query?query=up'
# up{job="node"}     1
# up{job="prometheus"} 1
# up{job="alertmanager"} 1
```

**这是刻意设计的**：`down` 删容器和网络（都是可重建的），不删卷（可能是不可重建的数据）。

要删卷必须显式：

```bash
docker compose down -v
```

### 卷生命周期表

| 命令 | 容器 | 网络 | 卷 |
|---|---|---|---|
| `restart` | 原地重启 | 保留 | 保留 |
| `stop` + `start` | 停再起 | 保留 | 保留 |
| `up -d`（改配置后）| 按 diff 重建 | 保留 | 保留 |
| `down` + `up` | 全部重建 | 重建 | **保留** |
| `down -v` + `up` | 全部重建 | 重建 | **删除** |

---

## 六、配置热加载：不重启容器生效

### 原理

配置是 bind mount 进去的，宿主机改了容器内立刻看到新内容。但**应用需要重新读取**。

Prometheus 支持两种方式：

```bash
# 方式 A：HTTP（需要 --web.enable-lifecycle）
curl -X POST localhost:19090/-/reload

# 方式 B：SIGHUP
docker kill --signal SIGHUP day9-prometheus
```

`--web.enable-lifecycle` 就是为此存在的，本栈已在 `command` 里打开：

```yaml
command:
  - --config.file=/etc/prometheus/prometheus.yml
  - --storage.tsdb.path=prometheus
  - --web.enable-lifecycle
```

### 实测：加规则不重启

执行前状态（uptime 3 天）：

```
NAME                 STATUS
day9-alertmanager    Up 3 days
day9-prometheus      Up 3 days (healthy)
...
```

追加规则：

```bash
cat >> prometheus/alert.rules.yml <<'EOF'

      - alert: HighLoadAverage
        expr: node_load1 > 2
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "1分钟负载超过 2"
EOF

curl -s -X POST localhost:19090/-/reload && echo "热加载成功"
# 热加载成功
```

执行后状态：

```
NAME                 STATUS
day9-alertmanager    Up 3 days        ← 仍是 3 days，没有归零
day9-prometheus      Up 3 days (healthy)
```

**`Up 3 days` 是证据**：容器从未重启，但规则已生效。

### 本实验发现的问题：重复规则不会报错

第一次成功加载后，又追加了一遍同样的规则，文件里出现了两个 `HighLoadAverage`（第 21 行和第 29 行）。

Prometheus **不报错**，静默加载了两条同名规则：

```
group: day9-demo | rules: ['NodeExporterDown', 'HighMemoryUsage', 'HighLoadAverage', 'HighLoadAverage']
```

这意味着**同一条告警会触发两次**，Alertmanager 会收到两份。

清理后：

```
group: day9-demo | rules: ['NodeExporterDown', 'HighMemoryUsage', 'HighLoadAverage']
```

⭐ **运维启示**：热加载"成功"只说明配置语法合法，不代表内容正确。改完规则应该核对**实际加载的条目**，而不是只看 reload 命令的返回值：

```bash
curl -s localhost:19090/api/v1/rules | python3 -c "import sys,json; d=json.load(sys.stdin); [print(g['name'], [r['name'] for r in g['rules']]) for g in d['data']['groups']]"
```

### 三层"改配置"的生效方式

| 改了什么 | 该用什么 | 容器是否重建 |
|---|---|---|
| 挂载进去的配置文件内容 | `curl -X POST /-/reload` | ❌ 不重建 |
| compose.yml（端口/环境变量/镜像） | `docker compose up -d` | 只重建有变化的服务 |
| 需要完全重来 | `docker compose up -d --force-recreate` | 强制重建，保留卷 |

⚠️ 只改挂载文件内容时，`up -d` **不会重建容器**——Compose 比对的是配置，不是文件内容。

---

## 七、restart 策略：docker kill 不触发重启

这一节是本次实验里**最容易讲错**的知识点。

### 实测矩阵（Docker 29.8.1）

| 容器怎么"死"的 | `restart: always` | `restart: unless-stopped` |
|---|---|---|
| `docker kill` / `docker stop` | ❌ 不重启 | ❌ 不重启 |
| 宿主机 `kill -9 <PID>` | ✅ 重启，RestartCount+1 | ✅ 重启，RestartCount+1 |
| 容器内 PID 1 自己退出（exit 0/1） | ✅ 重启 | ✅ 重启 |

### 证据

**`docker kill` 对两种策略都不重启：**

```bash
$ docker run -d --name k1 --restart always alpine sleep 3600
$ docker kill k1
$ docker inspect k1 --format "Status={{.State.Status}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}}"
Status=exited ExitCode=137 RestartCount=0
```

`unless-stopped` 同样：`Status=exited ExitCode=137 RestartCount=0`。

**宿主机 kill -9 则两种策略都重启：**

```bash
$ P=$(docker inspect -f '{{.State.Pid}}' t-always); kill -9 $P
$ docker inspect t-always --format "Status={{.State.Status}} RestartCount={{.RestartCount}}"
Status=running RestartCount=1
```

**容器内 PID 1 自杀（`sleep 5; exit 1`）：**

```
Status=running ExitCode=0 RestartCount=3
```

### 为什么

`docker kill` 和 `docker stop` 都走 Docker daemon 的 API。daemon 知道这是**用户主动停止**，会把容器记成 `manually stopped`，此时**任何 restart 策略都不生效**。

`docker kill` 虽然名字叫 kill、退出码也是 137（128+SIGKILL），但它是"受控停止"，不是"崩溃"。

**`always` 和 `unless-stopped` 的真正差异只在 daemon 重启时：**

| 策略 | dockerd 重启后 |
|---|---|
| `always` | 一律拉起，包括手动停过的 |
| `unless-stopped` | 只拉起没被手动停过的 |

生产上一般选 `unless-stopped`：服务崩溃自动恢复，但你停机维护后它不会在 daemon 重启时自己冒出来。

### 正确的"模拟崩溃"方法

```bash
PID=$(docker inspect -f '{{.State.Pid}}' day9-webhook)
kill -9 $PID
sleep 10
docker inspect day9-webhook --format "Status={{.State.Status}} RestartCount={{.RestartCount}}"
# Status=running RestartCount=1
```

⭐ **面试答法**：「restart 策略只对**非人为**退出生效。`docker stop`、`docker kill` 通过 daemon API 发起，会被标记为手动停止，任何策略都不重启。验证策略要绕过 daemon 杀掉容器 PID 1，或者让容器内主进程自己退出。`always` 和 `unless-stopped` 的差别只在 daemon 重启时的行为。」

⚠️ 不要用 137 判断"是不是被杀的"——宿主机 kill -9 和 `docker kill` 都是 137。

---

## 八、运维命令速查

```bash
# 启停
docker compose up -d                        # 启动/按 diff 更新
docker compose down                         # 停 + 删容器 + 删网络（保留卷）
docker compose down -v                      # 同时删卷
docker compose start|stop|restart <服务>     # 单服务

# 查看
docker compose ps                           # 运行中的
docker compose ps -a                        # 含已退出
docker compose logs -f --tail 50 <服务>
docker compose top
docker compose config                       # 展开完整配置，排查必跑

# 重建
docker compose up -d --force-recreate <服务>
docker compose pull && docker compose up -d
docker compose build <服务>

# 调试
docker compose exec <服务> sh
docker compose run --rm <服务> <命令>
```

### `docker compose config` 为什么必跑

它把 YAML 展开成 Docker 实际理解的形态：变量替换后、默认值填充后、相对路径转绝对路径后。排查"为什么挂载不对"、"为什么端口没生效"时，这是最快的定位手段。

实测输出片段：

```yaml
volumes:
  - type: bind
    source: /root/day9/monitoring/alertmanager/alertmanager.yml
    target: /etc/alertmanager/alertmanager.yml
    read_only: true
    bind: {}
```

`read_only: true` 就是 `:ro` 展开后的样子。

---

## 九、附：一次真实的故障排查

> 与 Compose 无关，但发生在同一台实验机上，案例价值很高。

### 现象

某天 SSH 连 ECS 后输入命令要等很久才显示，从本机 ping：

```
7 packets transmitted, 6 packets received, 14.3% packet loss
round-trip min/avg/max = 42.123/53.004/95.947 ms
```

14.3% 丢包能解释 SSH 卡（每次击键一个 TCP 包，丢包后要等重传），但登录后还发现：

```bash
$ uptime
 21:11:01 up 101 days, 5:40, 1 user, load average: 20.03, 19.02, 18.69
```

**load 20，而 CPU 占用看起来都不高。** 这是第一个关键点。

### 排查过程

```bash
$ ps aux --sort=-%cpu | head -20
root    1156633  0.9  2.0  ... DN   06:14  8:31  /usr/bin/python3 /usr/lib/update-notifier/apt-check --human-readable
root    1155912  0.1  4.8  ... Sl   06:08  1:37  /usr/bin/python3 /usr/bin/unattended-upgrade
```

两个进程从早上 6 点跑到现在，**约 15 小时**。注意 `apt-check` 的状态是 `DN`：

- `D` = uninterruptible sleep（等待 I/O）
- `N` = low priority

**load average 统计的是「运行中 + 不可中断睡眠」的进程数，不是 CPU 使用率。** 所以 D 状态进程堆积能把 load 拉很高，同时 CPU 很闲。

### 卡住的原因

```bash
$ lsof /var/lib/dpkg/lock-frontend
COMMAND       PID USER   FD   TYPE DEVICE SIZE/OFF   NODE NAME
unattende 1155912 root    8uW  REG  252,3        0 261976 /var/lib/dpkg/lock-frontend
```

`unattended-upgrade` 持有 dpkg 锁卡住，`apt-check` 等锁也卡住。**`kill` 和 `kill -9` 都无效**——D 状态进程在内核态等 I/O，信号根本没机会被处理。

### 根因：snapd 崩溃循环

日志才是真相：

```
06:08:05  apt-daily-upgrade.timer 触发
06:08:17  开始升级 15 个包，含 systemd .22 → .23
06:08:32  systemd 自我 re-exec
06:09:27  dpkg 安装全部完成        ← 包其实装好了
          ↓ 之后卡在不该卡的地方
06:16:46  snapd.service 第 48 次重启，"stop-sigterm timed out"
...
21:36:54  snapd.service 第 576 次重启
```

统计：**6:16 到 21:36 之间 snapd 重启了 529 次。**

`snapd.service` 的 unit 配置：

```ini
Restart=always
RestartUSec=100ms
WatchdogSec=5m
KillMode=process
```

`systemd` 换代后，snapd 启动持续超时，100ms 后就重试，每次又撞 5 分钟看门狗超时，形成无限循环。

死锁链条：

```
unattended-upgrade  持有 dpkg 锁，卡在 needrestart
      ↓
needrestart         卡在等 snapd 停下来（stop-sigterm 超时）
      ↓
snapd               100ms 一次疯狂重启，累积残留进程（KillMode=process 只杀主进程）
      ↓
apt-check           等 dpkg 锁 → D 状态
      ↓
SSH 登录            MOTD 调 apt-check → 又一个 D 状态
      ↓
load average → 20
```

### 解决

`kill -9` 无效后软重启（控制台），恢复：

```
load average: 0.14
snapd NRestarts=0
df -h /  25%
```

重启清掉了三样东西：陈旧的 snapd 状态锁、堆积的残留进程、卡住的 needrestart 等待队列。

### 验证不是常态问题

隔离 3 天 22 小时后复查：

```
up 3 days, 22:54   load average: 0.27, 0.13, 0.10
snapd NRestarts=0
```

`apt-daily-upgrade.timer` 期间又跑过一轮（06:36），**没有再卡死**。确认是 systemd 跨版本升级引发的一次性问题。

### 这条案例的面试价值

1. **load average ≠ CPU 使用率**。它含 D 状态进程，I/O 卡死能把 load 拉高而 CPU 空闲。
2. **D 状态进程 kill -9 杀不掉**。信号在内核态被阻塞，只能等 I/O 超时或重启。
3. **"包装完了"不等于"升级成功"**。dpkg 阶段全绿，卡在之后的 post-install 服务重启。
4. **排查看日志时间线**。`journalctl --since/--until` 定位到具体卡在 06:09—06:16 之间，比看 `ps` 快得多。
5. **重启前先想清楚会不会再犯**。这次靠时间线确认是一次性触发，否则重启只是掩盖问题。

---

## 十、本次实验修正的既有认知

| 原以为 | 实测结果 |
|---|---|
| `docker kill` 模拟崩溃，restart 策略会拉起 | **不会**。`docker kill` 被标记为手动停止，任何策略都不重启 |
| `always` 比 `unless-stopped` 更"积极" | 差异**只在 dockerd 重启时**；崩溃场景两者行为一致 |
| 退出码 137 说明容器是被杀的 | 宿主机 `kill -9` 和 `docker kill` 都是 137，无法区分 |
| `depends_on` 表示"依赖就绪后才启动" | 只保证启动顺序，**不保证就绪**；要 `condition: service_healthy` |
| 健康检查失败会自动重启 | 不会。只改状态为 `unhealthy`，那是 K8s livenessProbe 的行为 |
| 健康检查 `ExitCode: -1` 表示检查失败 | `-1` 是**命令不存在**（OCI exec 失败），非 0 才是命令跑了但失败 |
| 热加载成功 = 配置正确 | 只代表语法合法。重复规则名会被静默加载，告警触发两次 |
| load average 高 = CPU 忙 | load 含 D 状态进程，I/O 或锁等待也能拉高 |
