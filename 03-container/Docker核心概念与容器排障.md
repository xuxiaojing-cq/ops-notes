# Docker 核心概念与容器排障

> 实验环境：Ubuntu 22.04 / 2vCPU / 1.6G 内存 / Swap 0
> Docker Engine 29.8.1，镜像 `nginx:latest`、`alpine:latest`
> 本文所有输出均为实机执行结果，非示意。

---

## 一、容器到底是什么

一句话：**容器不是轻量虚拟机，是被 namespace 隔离、被 cgroup 限额的宿主机普通进程。**

| | 虚拟机 | 容器 |
|---|---|---|
| 内核 | 每个 VM 一个独立内核 | ⭐ **共享宿主机内核** |
| 隔离手段 | Hypervisor 硬件虚拟化 | namespace（看不见）+ cgroup（用不多） |
| 启动 | 秒级到分钟级 | 毫秒级（就是 fork 一个进程） |
| 宿主机视角 | 一个 qemu 进程 | ⭐ **一个普通进程，`ps` 直接能看到** |

⚠️ **共享内核**推出两个重要结论，面试常考：

1. Linux 容器跑不了 Windows 程序，内核 ABI 不同。
2. **容器逃逸 = 拿到宿主机 root**，因为中间没有 Hypervisor 这层硬隔离。这也是 `--privileged` 危险的根本原因。

### 同一个进程，两个 PID

前面实验里最直观的一组数据：

```
容器内：  PID 1     sleep 3600
宿主机：  PID 958494 sleep 3600
```

不是两个进程，是**一个进程在两个 PID namespace 里的两个编号**。容器里之所以看到自己是 PID 1，是因为 PID namespace 给它重新编了号。

⭐ **由此引出「PID 1 问题」**：PID 1 在 Linux 里有特殊语义——不会接收未注册处理函数的默认信号、要负责回收孤儿进程。所以容器主进程如果是 shell 脚本，很容易出现「`docker stop` 不生效，等 10 秒后被 SIGKILL」和「僵尸进程堆积」。解决办法是 `docker run --init` 或用 `tini` 做 PID 1。

---

## 二、cgroup 与 OOM：`137` 和 `OOMKilled` 是两回事

这是本次实验最有价值的一组对照，**两个方向的陷阱都撞到了**。

### 对照实验一：同样 OOM，退出码不同

| 容器 | 命令形态 | OOMKilled | ExitCode |
|---|---|---|---|
| `oomtest` | `sh -c "sleep 5; dd ...; sleep 100"` | `true` | ⚠️ **`0`** |
| `oomtest2` | `dd ...`（PID 1 就是它） | `true` | `137` |

**为什么同样被 OOM，退出码一个 0 一个 137？**

容器的退出码 = **PID 1 的退出码**。

- `oomtest2` 里 PID 1 就是 `dd`，`dd` 被 SIGKILL → 128+9 = **137**。
- `oomtest` 里 PID 1 是 `sh`，被杀的是它的子进程 `dd`。`sh` 发现子进程死了，继续往下执行 `sleep 100`……但内存已被 `dd` 撑爆，实际是 `sh` 正常走完退出 → **0**。

⚠️ **所以「看到 137 就是 OOM」是错的，「不是 137 就没 OOM」更错。**

### 对照实验二：137 也可能不是 OOM

实测 `docker kill`（发 SIGKILL）：

```
$ docker run -d --name t-kill alpine sleep 300 && docker kill t-kill
$ docker inspect -f '{{.State.ExitCode}} OOMKilled={{.State.OOMKilled}}' t-kill
137 OOMKilled=false        ← ⭐ 137 但没 OOM
```

还有一个反直觉的实测：

```
$ docker run -d --name t-stop alpine sleep 300 && docker stop t-stop
$ docker inspect -f '{{.State.ExitCode}}' t-stop
137                        ← ⚠️ docker stop 也是 137，不是 143
```

**为什么 `docker stop` 不是 143？** `docker stop` 先发 SIGTERM，等 10 秒宽限期；`alpine` 的 `sleep` 作为 PID 1 **不处理 SIGTERM**（PID 1 对未注册处理函数的信号免疫），10 秒后 Docker 补一刀 SIGKILL → 137。

⭐ **这条实测直接串起了上面的「PID 1 问题」**：如果应用正确处理了 SIGTERM，`docker stop` 才会得到 143 并优雅退出。**生产上看到大量 137 且伴随 `docker stop` 耗时正好 10 秒，基本就是应用没处理 SIGTERM。**

### 结论：判断 OOM 只认一个字段

```bash
docker inspect -f '{{.State.OOMKilled}}' <容器>
```

配合宿主机侧证据：

```bash
dmesg -T | grep -i "killed process"
journalctl -k | grep -i oom
```

---

## 三、退出码速查（全部实测）

| ExitCode | 含义 | 实测命令 |
|---|---|---|
| `0` | 正常结束 | — |
| `3`（任意 1-124） | ⭐ **应用自己的退出码，原样透传** | `alpine sh -c "exit 3"` |
| `125` | ⭐ **Docker 自身的错**（run 参数非法、挂载配置无效） | `docker run --badflag` / `--mount` 源不存在 |
| `126` | 命令存在但**不可执行** | `alpine /etc/hostname` → `permission denied` |
| `127` | ⭐ **命令不存在**（镜像里没这个二进制，最常见） | `alpine nosuchcmd` → `executable file not found in $PATH` |
| `137` | 128+9，被 SIGKILL（OOM、`docker kill`、**stop 超时**） | 见上节 |
| `143` | 128+15，被 SIGTERM 且**应用自己响应了** | — |

⚠️ **125 / 126 / 127 三者的分界很实用**：

- `125` → **容器根本没起来**，是 Docker 层面就拒绝了，查你的 `docker run` 参数。
- `126` / `127` → **容器起来了，但 PID 1 拉不起来**，查镜像里的二进制、`ENTRYPOINT`/`CMD` 拼写、脚本有没有可执行位。

⭐ `127` 在交付现场特别高频，典型原因：镜像是 alpine（musl）但二进制是 glibc 编译的、或者 shell 脚本第一行 shebang 写了 `/bin/bash` 而 alpine 里只有 `/bin/sh`。

---

## 四、数据持久化：一个斜杠的差别

```bash
docker run -v /data:/data nginx    # ← bind mount（第一个字符是 /）
docker run -v  data:/data nginx    # ← named volume
```

| | bind mount | named volume |
|---|---|---|
| 数据位置 | 宿主机指定路径 | `/var/lib/docker/volumes/<名>/_data` |
| 源不存在时 | ⚠️ **静默创建空目录** | 自动创建卷 |
| 镜像里原有文件 | **被完全遮盖** | ⭐ **首次挂载时复制进卷** |
| `docker volume ls` | 看不到 | 看得到 |
| 生命周期 | 自己管 | Docker 管 |
| 适合 | 挂配置、挂日志目录、开发调试 | 生产数据（数据库等） |

### 实测：卷会复制镜像内容，bind mount 会遮盖

```bash
$ docker run --rm -v testvol:/etc/nginx nginx ls /etc/nginx
conf.d
fastcgi_params
mime.types
modules
nginx.conf
scgi_params
uwsgi_params

$ mkdir -p /tmp/emptydir
$ docker run --rm -v /tmp/emptydir:/etc/nginx nginx ls /etc/nginx
（无任何输出，退出码 0）
```

**同一个镜像、同一个挂载点，一个能看到 7 个文件，一个啥也没有。**

⚠️ 「复制」**只在卷为空时发生一次**。卷里已有数据不会被覆盖——这是为了避免升级镜像时冲掉生产数据。反过来说，**改了镜像里的默认配置后发现容器里还是旧的，先看是不是挂了个老卷。**

### ⭐ `-v` vs `--mount`：不是语法糖，是安全边界

实测两条命令，源路径都不存在：

```bash
$ docker run --rm --mount type=bind,source=/nonexistent,target=/data alpine ls /data
docker: Error response from daemon: invalid mount config for type "bind":
        bind source path does not exist: /nonexistent
[exit=125]                         ← ⭐ 直接拒绝启动

$ docker run --rm -v /nonexistent2:/data alpine ls /data
[exit=0]                           ← ⚠️ 成功了，但 /data 是空的
$ ls -ld /nonexistent2
drwxr-xr-x 2 root root 4096 Sep 23 16:41 /nonexistent2    ← Docker 替你建的
```

**这就是那个经典坑的完整复现**：想挂配置文件但路径写错 → `-v` 悄悄建一个**同名空目录** → 容器里 `/etc/nginx/nginx.conf` 变成一个**目录** → nginx 启动报 `is a directory`。

**排查线索**：容器起不来、日志说某配置文件是目录 → 去宿主机 `ls -ld` 那个路径，看到一个刚创建的空目录，属主 root、时间就是刚才。

⭐ **面试答「`-v` 和 `--mount` 的区别」，说「语法更清晰」是背书；说「`--mount` 把静默创建空目录这个坑变成 125 启动失败」才是用过。**

### 第三种：tmpfs

```bash
docker run --rm --tmpfs /tmp:size=64m alpine df -h /tmp
```

存内存、不落盘、容器停即消失。用于敏感临时数据。

### 容器日志会把磁盘写满

⚠️ **默认 json-file driver 不限制大小**，这是生产事故常见原因。

```bash
$ ls -lh /var/lib/docker/containers/<容器ID>/<容器ID>-json.log
-rw-r----- 1 root root 2.6K Sep 23 16:44 .../95f8c9a3...-json.log
```

全局限制写 `/etc/docker/daemon.json`（改完 `systemctl restart docker`，**只对新建容器生效**）：

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "100m", "max-file": "3" }
}
```

⭐ **容器化环境磁盘打满，前三个嫌疑永远是：容器日志、镜像堆积、可写层膨胀。** 一条命令全看到：

```bash
$ docker system df
TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          2         1         254.7MB   241.6MB (94%)
Containers      4         0         32.77kB   32.77kB (100%)
Local Volumes   1         1         9.394kB   0B (0%)
Build Cache     0         0         0B        0B
```

`RECLAIMABLE 94%` = 241MB 镜像没被任何容器使用。`docker system prune -a` 可回收，⚠️ **生产上加 `-a` 前务必确认没有「停着但还要用」的容器**，否则它依赖的镜像会被一起删掉。

---

## 五、容器网络

### 三种默认模式

| 模式 | 网络栈 | 端口 | 场景 |
|---|---|---|---|
| `bridge`（默认） | 独立 NET namespace + 虚拟网桥 `docker0` | 需 `-p` 映射 | 绝大多数 |
| `host` | ⭐ **共享宿主机网络栈** | 直接用宿主机端口，`-p` 无效 | 极致网络性能、抓宿主机网卡 |
| `none` | 只有 lo | 无 | 纯计算、安全隔离 |

### 实测：host 模式就是同一个 namespace

```
host 容器  : net:[4026531840]
bridge 容器: net:[4026532241]
宿主机 PID1: net:[4026531840]
```

⭐ **host 容器和宿主机 PID 1 的 netns inode 完全相同（4026531840）**，bridge 容器则是另一个号。这不是「网络配置一样」，是**物理上就是同一个 network namespace**——所以 `host` 模式下 `-p` 无效（没有需要转发的边界），端口冲突也会直接和宿主机撞。

### 端口映射的真相：iptables DNAT + docker-proxy

```bash
$ docker run -d --name t-web -p 8080:80 nginx
$ curl -s localhost:8080 | head -3
<!DOCTYPE html>
<html>
<head>

$ iptables -t nat -L DOCKER -n
Chain DOCKER (2 references)
DNAT  tcp  --  0.0.0.0/0   0.0.0.0/0   tcp dpt:8080 to:172.17.0.3:80
```

⭐ **`-p` 的本质是内核 NAT 规则**：发往宿主机 8080 的包，目标地址被改写成容器 `172.17.0.3:80`。

但实测还看到了第二个机制：

```bash
$ ss -lntp | grep 8080
LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:(("docker-proxy",pid=970691,fd=8))
LISTEN 0 4096    [::]:8080    [::]:* users:(("docker-proxy",pid=970698,fd=8))
```

**有一个真实的 `docker-proxy` 用户态进程在监听 8080。** 两套机制并存：

- **iptables DNAT** 处理绝大多数外部流量，走内核转发，性能好。
- **`docker-proxy`** 兜底处理 DNAT 覆盖不到的场景（典型是宿主机本地 `localhost` 回环访问，本机发起的包在某些路径上不经过 `PREROUTING`）。

⚠️ **每个映射端口都会起一个 docker-proxy 进程**，映射端口特别多时会占掉可观内存。可在 `daemon.json` 里设 `"userland-proxy": false` 关掉。

### ⚠️ Docker 绕过 ufw —— 云主机被入侵的头号原因

Docker 把规则直接插进 iptables 的 `DOCKER` 链，**位置在 ufw 的 `INPUT` 规则之前**。所以 ufw 里封了的端口，`-p` 照样对公网开放。

本机实测 `ufw status` 是 `inactive`，没能直接演示这个冲突，但**规则链顺序决定了这个结论成立**，且 `-p 8080:80` 实际绑定的是 `0.0.0.0`（见上面 `ss` 输出），验证了「默认对所有网卡开放」这半边。

⭐ 安全写法 —— **显式只绑回环**：

```bash
docker run -d -p 127.0.0.1:8080:80 nginx
```

⚠️ 典型事故：`-p 3306:3306`、`-p 6379:6379` 映射到公网且无密码，几小时内就会被扫到。**云主机上真正的防线是安全组，不要指望 ufw 挡住 Docker。**

### ⭐ 自定义网络：默认 bridge 没有 DNS

```bash
$ docker run -d --name c1 alpine sleep 300
$ docker run --rm alpine ping -c1 c1
ping: bad address 'c1'                          ← ⚠️ 默认 bridge 解析不了容器名

$ docker network create mynet
$ docker run -d --name c2 --network mynet alpine sleep 300
$ docker run --rm --network mynet alpine ping -c1 c2
PING c2 (172.18.0.2): 56 data bytes
64 bytes from 172.18.0.2: seq=0 ttl=64 time=0.240 ms    ← ✅ 通了
```

**原因在 `/etc/resolv.conf`，一眼看穿：**

```bash
$ docker exec c1 cat /etc/resolv.conf      # 默认 bridge
nameserver 100.100.2.136                   ← 直接抄宿主机的 DNS
nameserver 100.100.2.138

$ docker exec c2 cat /etc/resolv.conf      # 自定义网络
nameserver 127.0.0.11                      ← ⭐ Docker 内嵌 DNS
options timeout:2 attempts:3 rotate single-request-reopen ndots:0
```

⭐ **容器名能解析不是「容器自带的能力」，是 Docker 往自定义网络的容器里塞了一个内嵌解析器 `127.0.0.11`**，由它维护「容器名 → IP」的映射。默认 bridge 上没有这东西，只能靠 IP 通信——而容器 IP 每次重建都会变，所以**生产上永远不要用默认 bridge**。

> 这也是 Day 9 用 Compose 编排监控栈的基础：Compose 自动为每个项目建一个自定义网络，所以 `prometheus.yml` 里能直接写 `targets: ['node-exporter:9100']` 而不用查 IP。

另外注意网段：默认 bridge 是 `172.17.0.0/16`，自定义网络 `mynet` 自动分到 `172.18.0.0/16`。⚠️ **交付现场如果内网网段和 `172.17/172.18` 重叠，会出现「装了 Docker 之后某些内网地址访问不通」**，需要在 `daemon.json` 里用 `bip` 和 `default-address-pools` 改掉。

---

## 六、排障五件套

| 命令 | 看什么 | 前提 |
|---|---|---|
| `docker logs` | 应用自己说了什么 | **永远第一步**，容器死了也能看 |
| `docker inspect` | Docker 眼里的配置与状态 | 任何时候 |
| `docker exec` | 运行时真实环境 | ⚠️ 容器**必须活着** |
| `docker stats` | 实时 CPU / 内存 / IO | 容器活着 |
| `docker top` | 容器内进程 + 宿主机 PID | 容器活着 |

### 1. `logs`：容器死了日志还在

```bash
docker logs --tail 50 -f <容器>       # 跟随
docker logs --since 10m <容器>        # 最近 10 分钟
docker logs -t <容器>                 # 带时间戳
```

实测：`t-kill` 已被 kill 退出，`docker logs` 仍正常返回（exit=0）。日志文件在 `/var/lib/docker/containers/<id>/<id>-json.log`，**只要容器没被 `rm`，日志就在**。

⭐ **所以容器起不来时最忌讳 `docker rm` 重来** —— 一删，尸检报告就没了。正确顺序是 `logs` → `inspect` → 再决定删不删。

⚠️ `docker logs` **只能看 stdout/stderr**。应用把日志写进容器内文件的话，这条命令什么都看不到——这是「别在容器里写日志文件」的另一个理由。

### 2. `inspect`：用 `-f` 精确取值

```bash
docker inspect -f '{{.State.Status}}'    <容器>   # running / exited
docker inspect -f '{{.State.ExitCode}}'  <容器>
docker inspect -f '{{.State.OOMKilled}}' <容器>   # ⭐ 判断 OOM 只认它
docker inspect -f '{{.State.Pid}}'       <容器>   # 宿主机 PID
docker inspect -f '{{.RestartCount}}'    <容器>   # 重启过几次
docker inspect -f '{{json .Mounts}}'     <容器> | python3 -m json.tool
```

⚠️ **实测踩到一个坑**：

```bash
$ docker inspect -f '{{.NetworkSettings.IPAddress}}' t-web
template parsing error: map has no entry for key "IPAddress"
```

顶层 `.NetworkSettings.IPAddress` **在新版 Docker（本机 29.8.1）上已经取不到了**，IP 挪进了 `.NetworkSettings.Networks.<网络名>.IPAddress`。容器可能同时接多个网络，顶层单个 IP 字段本身就表达不了。正确写法：

```bash
$ docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} => {{$v.IPAddress}}{{"\n"}}{{end}}' t-web
bridge => 172.17.0.3
```

⭐ 网上大量老文档还在用顶层写法，**照抄会静默拿到空字符串**（如果用在脚本里更隐蔽）。

### 3. `exec`：三个拦路虎

```bash
docker exec -it <容器> bash      # nginx 等完整镜像
docker exec -it <容器> sh        # ⚠️ alpine 没有 bash
docker exec <容器> env           # 不进去，直接看环境变量
```

1. **容器已退出 → `exec` 直接失败**。改用 `docker run -it --entrypoint sh <镜像>` 起个壳子进去看镜像内容。
2. **精简镜像没 shell**。`distroless` / `scratch` 里连 `sh` 都没有，只能靠 `docker cp` 把文件捞出来看。
3. ⭐ **`exec` 里装的东西、改的配置，容器重建就没了**（写在可写层）。**生产上绝不能靠 `exec` 改配置**——这是容器化最关键的思维转变：**容器不可变，改配置要么改镜像，要么改挂载。**

### 4. `stats` / `top` / cgroup 反查

```bash
$ docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
NAME       CPU %     MEM USAGE / LIMIT     MEM %
t-web      0.00%     9.277MiB / 1.571GiB   0.58%
c1         0.00%     1.406MiB / 1.571GiB   0.09%
```

⚠️ **`LIMIT` 显示 1.571GiB 是宿主机总内存，不是容器限额** —— 因为这些容器没设 `-m`。看到这个数字不要以为「容器可以用 1.5G」，它只是说「没限制，能抢多少是多少」。**生产上必须显式 `-m`，否则一个容器 OOM 可能拖垮整台机器。**

```bash
$ docker top t-web
UID       PID      PPID     CMD
root      970661   970636   nginx: master process nginx -g daemon off;
systemd+  970735   970661   nginx: worker process
systemd+  970736   970661   nginx: worker process
```

⭐ `docker top` 给的是**宿主机视角的 PID**，可以直接和 `htop`、`ps aux` 对上号。

**反向查（排查「宿主机上这个吃 CPU 的进程属于哪个容器」）：**

```bash
$ cat /proc/970661/cgroup
0::/system.slice/docker-95f8c9a3928bd368b2ca6e7cb785f4aee0545c416201f197df171440f83cb602.scope
```

⭐ **cgroup 路径里就是完整容器 ID**，拿它 `docker inspect` 或 `docker ps -a | grep 95f8c9a` 即可定位。这是从「宿主机异常」倒查到「哪个容器干的」的标准路径。

### 5. 完整排障链路

```
1. docker ps -a        → 状态、退出码是多少
2. docker logs         → 应用自己报了什么错
3. docker inspect      → OOMKilled? RestartCount? Mounts 挂对了吗?
4. 容器活着 → docker exec 进去看真实环境
   容器死了 → docker run -it --entrypoint sh <镜像> 起壳子看
5. 怀疑资源 → docker stats / 宿主机 free、df / docker system df
```

⭐ **这个顺序的逻辑和 Linux 分层排除法一致：按「取证成本」递增排序。** `logs` 零成本且不影响现场；`exec` 要求容器活着；`--entrypoint sh` 要重启一次、可能破坏现场。面试时把**排序理由**讲出来，比背命令清单有说服力得多。

---

## 七、本次实验修正的两个既有认知

记录下来，因为这两条是查资料时常见的错误说法：

| 常见说法 | 实测结果 |
|---|---|
| 「`docker stop` 退出码是 143」 | ⚠️ **不一定**。应用不处理 SIGTERM 时，10 秒后被 SIGKILL，退出码是 **137** |
| 「`docker inspect -f '{{.NetworkSettings.IPAddress}}'` 取容器 IP」 | ⚠️ **新版取不到**，需走 `.NetworkSettings.Networks.<网络名>.IPAddress` |

---

## 八、速查

```bash
# 状态与退出原因
docker ps -a
docker inspect -f '{{.State.Status}} {{.State.ExitCode}} OOM={{.State.OOMKilled}} restarts={{.RestartCount}}' <容器>

# 日志
docker logs --tail 100 --since 30m -t <容器>

# 网络
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' <容器>
iptables -t nat -L DOCKER -n
docker exec <容器> cat /etc/resolv.conf

# 资源
docker stats --no-stream
docker system df
docker top <容器>
cat /proc/<宿主机PID>/cgroup        # 反查容器

# 清理（⚠️ -a 会删掉未被使用的镜像）
docker container prune -f
docker system df && docker system prune -a
```
