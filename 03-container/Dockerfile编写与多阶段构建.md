# Dockerfile 编写与多阶段构建

> 实验环境：Ubuntu 22.04 / Docker Engine 29.8.1 / BuildKit 默认构建器
> 基础镜像：`alpine:latest`、`python:3.12-slim`、`golang:1.22-alpine`
> 本文所有关键数字均为实机执行结果，非示意。

---

## 一、镜像分层：为什么删了文件镜像不变小

Docker 镜像不是一个完整目录的压缩包，而是一组只读层叠加出来的文件系统。运行容器时，Docker 再在最上面加一层可写层。

关键结论：**已经生成的层不可变。后续 `rm` 只是写入 whiteout 标记把文件遮住，不能把下层数据真正删除。**

### 实测：50MiB 文件删掉后是否还占空间

对照两个 Dockerfile：

```dockerfile
# Dockerfile.bad
FROM alpine
RUN dd if=/dev/zero of=/big bs=1M count=50
RUN rm /big
```

```dockerfile
# Dockerfile.good
FROM alpine
RUN dd if=/dev/zero of=/big bs=1M count=50 && rm /big
```

构建后精确体积：

| 镜像 | 精确字节 | 现象 |
|---|---:|---|
| `layer:bad` | `65,420,609` | 两层：先写入 50MiB，再在下一层删除 |
| `layer:goodref` | `12,936,241` | 同一层内写入并删除 |
| `alpine:latest` | `12,932,203` | 基准 |

`layer:bad` 比 `layer:goodref` 多 `52,488,406` 字节，约等于 50MiB。

容器内再看：

```bash
$ docker run --rm layer:bad ls -l /big
ls: /big: No such file or directory
```

文件在最终视图里确实不存在，但数据仍留在旧层里。

### 面试答法

- Docker 镜像是分层的，层不可变。
- 后续 `rm` 不能删除旧层内容，只能在新层写 whiteout 标记隐藏它。
- 所以安装和清理必须写在**同一条 `RUN`** 里：

```dockerfile
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*
```

---

## 二、多阶段构建：把编译环境扔掉

构建需要的东西和运行需要的东西通常不是一回事。Go 程序编译需要 `golang` 镜像，但运行时只需要编译好的二进制文件。

### 实测体积对比

同一个 Go HTTP 服务，三种构建方式：

| 构建方式 | 精确字节 | 显示体积 | 说明 |
|---|---:|---:|---|
| `goapp:single` | `456,441,148` | 456MB | 单阶段，最终镜像包含编译器和构建缓存 |
| `goapp:multi` | `19,800,889` | 19.8MB | 多阶段构建，最终基于 alpine |
| `goapp:scratch` | `6,869,758` | 6.87MB | 多阶段构建，最终基于 scratch |
| `golang:1.22-alpine` | — | 349MB | 构建阶段基础镜像 |

注意：`goapp:single` 比 `golang:1.22-alpine` 还大。多出来的部分主要是 `go build` 缓存、`go mod` 下载缓存和源码等构建中间产物。

### 推荐写法

```dockerfile
FROM golang:1.22-alpine AS builder
WORKDIR /src
COPY go.mod .
RUN go mod download
COPY main.go .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /hello .

FROM alpine:latest
COPY --from=builder /hello /hello
EXPOSE 8000
CMD ["/hello"]
```

### 面试答法

- 多阶段构建用前面的阶段编译，用最后阶段运行。
- `COPY --from=builder` 只把产物复制到最终镜像，编译器、源码和缓存不会进入最终镜像。
- 实测从 456MB 降到 19.8MB，约 23 倍。
- `scratch` 更小，但没有 shell、包管理器、证书和排障工具；线上常用 alpine 或 distroless 在体积和可排障性之间折中。
- Go 程序如果想放进 alpine/scratch，通常要注意 `CGO_ENABLED=0`，避免动态链接导致运行时报错。

---

## 三、层缓存与 COPY 顺序

Docker 构建缓存的核心规则：每一步缓存键依赖父层、指令文本，以及 `COPY`/`ADD` 涉及文件的内容校验和。**上游某层失效，后续所有层都会失效。**

因此 Dockerfile 的排序原则是：**不常变的放上面，经常变的放下面。**

### 错误写法

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "app.py"]
```

改一行业务代码，`COPY . .` 失效，后面的 `pip install` 也必须重跑。

### 正确写法

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["python", "app.py"]
```

只改业务代码时，依赖清单没变，`pip install` 可以命中缓存。

### 实测：flask + requests

只改一行 `app.py` 后重新构建：

| 写法 | 耗时 | 关键构建行为 |
|---|---:|---|
| `COPY . .` 在 `pip install` 前 | `2m43.514s` | `pip install` 重跑 |
| 先 `COPY requirements.txt` | `1.777s` | `pip install` 命中缓存 |

差距约 92 倍。

### 本次实验修正

第一次用无第三方依赖的 Go 项目测试，`33.958s` vs `33.070s`，几乎没有差距。原因是 `go mod download` 本来就是空操作，真正耗时的是 `go build`，而改了源码后 `go build` 必然重跑。

所以更准确的说法是：**优化 COPY 顺序节省的是依赖安装/下载那一步的耗时。依赖越重，收益越大；没有依赖时收益很小。**

---

## 四、CMD、ENTRYPOINT 与优雅退出

### CMD 和 ENTRYPOINT 的分工

| | ENTRYPOINT | CMD |
|---|---|---|
| 定位 | 固定可执行主体 | 默认参数 |
| `docker run 镜像 xxx` | 一般不被替换 | 会被 `xxx` 替换 |
| 覆盖方式 | `--entrypoint` | 命令行直接覆盖 |

推荐模式：

```dockerfile
ENTRYPOINT ["/app"]
CMD ["--port", "8080"]
```

### exec 形式和 shell 形式

```dockerfile
CMD ["python", "app.py"]      # exec 形式
CMD python app.py             # shell 形式，会被改写为 /bin/sh -c
```

Docker build 对 shell 形式会给出警告：

```text
JSONArgsRecommended: JSON arguments recommended for CMD to prevent unintended behavior related to OS signals
```

原因是 shell 形式可能让 `/bin/sh` 成为 PID 1，导致信号无法传给真正的应用。

### 一个容易讲错的细节：shell 可能会 exec 自替换

本次实测发现：

```dockerfile
CMD sleep 300
```

镜像元数据确实是：

```text
["/bin/sh","-c","sleep 300"]
```

但实际容器 PID 1 是 `sleep 300`。原因是 shell 发现这是单一简单命令，会直接 `exec` 替换自己。

复杂命令则不同：

```dockerfile
CMD sleep 300 && echo never-reached
```

实测进程树：

```text
PID 1  /bin/sh -c sleep 300 && echo never-reached
PID 7  sleep 300
```

所以准确结论是：**shell 形式不一定让 sh 占住 PID 1；但一旦用了 `&&`、`;`、管道、重定向等复杂 shell 语法，sh 往往会留下来。生产里 shell 形式常常正是这种复杂命令，因此风险很高。**

### SIGTERM 优雅退出实测

Python 程序注册了 SIGTERM 处理函数：

```python
def bye(s, f):
    print("got SIGTERM, exiting gracefully", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
```

三种启动方式对比：

| 容器 | PID 1 | `docker stop` 耗时 | ExitCode | 日志 |
|---|---|---:|---:|---|
| `CMD ["python", "/app.py"]` | `python /app.py` | `0.262s` | `0` | `got SIGTERM, exiting gracefully` |
| `CMD python /app.py && echo never` | `/bin/sh -c ...` | `10.351s` | `137` | 没有 SIGTERM 日志 |
| 上一项加 `--init` | `/sbin/docker-init -- /bin/sh -c ...` | `0.315s` | `143` | 没有 SIGTERM 日志 |

结论：

1. exec 形式下，应用作为 PID 1，能直接收到 SIGTERM 并优雅退出。
2. 复杂 shell 形式下，SIGTERM 被发给 sh，真正应用收不到，10 秒后被 SIGKILL，退出码 137。
3. `--init` 能让容器快速停止并回收子进程，但不等于应用一定收到了 SIGTERM。本次实测中 python 仍没有打印 SIGTERM 日志。

### 面试答法

- Docker/Kubernetes 停容器时先发 SIGTERM，等待宽限期，再发 SIGKILL。
- 如果应用收不到 SIGTERM，滚动发布时可能中断正在处理的请求。
- 推荐使用 exec 形式的 `CMD`/`ENTRYPOINT`，并让应用处理 SIGTERM。
- `--init` 主要解决 PID 1 和僵尸进程问题，但不能替代正确的入口命令和应用信号处理。

补充实测：`docker stop` 对已经退出的容器是幂等空操作，几十毫秒返回且不改变原退出码。因此 `docker stop` 秒回不一定代表应用优雅退出，也可能是容器本来已经退出。

---

## 五、构建上下文与 .dockerignore

`docker build .` 里的 `.` 是构建上下文。Docker CLI 会把上下文打包发送给 daemon，再执行 `COPY`/`ADD`。

如果上下文里有 `.git`、`.env`、日志、依赖目录，都会被打包。若 Dockerfile 里写了 `COPY . .`，它们还可能进入镜像。

### 实测：20MB 的 .git 和 .env

上下文里放入：

```text
.git/pack.bin        20MB
.env                 DB_PASSWORD=supersecret123
__pycache__/
```

无 `.dockerignore` 时：

```text
transferring context: 20.98MB
```

镜像内文件：

```text
.env
.git
Dockerfile
Dockerfile.copyall
__pycache__
app.py
requirements.txt
```

`.env` 内容可直接读出：

```bash
$ docker run --rm webapp:dirty cat /app/.env
DB_PASSWORD=supersecret123
```

加入 `.dockerignore`：

```dockerignore
.git
.env
.env.*
**/__pycache__
**/*.pyc
Dockerfile*
```

再次构建：

```text
transferring context: 154B
```

镜像里只剩：

```text
.dockerignore
app.py
requirements.txt
```

体积对比：

| 镜像 | 精确字节 |
|---|---:|
| `webapp:dirty` | `198,305,786` |
| `webapp:clean` | `177,297,125` |

### 面试答法

- `.dockerignore` 在打包上下文阶段生效，不是 COPY 时才过滤。
- 它同时解决三个问题：构建慢、缓存容易失效、敏感文件泄漏。
- `COPY . .` 必须配合 `.dockerignore`，否则 `.env`、`.git`、私钥可能被打进镜像。

推荐最小模板：

```dockerignore
.git
.env
.env.*
**/__pycache__
**/*.pyc
node_modules
.venv
*.log
*.pem
*.key
id_rsa*
Dockerfile*
```

---

## 六、ARG、ENV 与密钥泄漏

`ARG` 只在构建期生效，`ENV` 会进入最终镜像和运行时环境。但这不代表 `ARG` 适合传密钥。

### 实测：build-arg 明文进入 history

Dockerfile：

```dockerfile
FROM alpine
ARG SECRET=default_value
RUN echo "using $SECRET" > /tmp/x
ENV VISIBLE=$SECRET
```

构建：

```bash
docker build --build-arg SECRET=from_cmdline -t argtest .
```

`docker history --no-trunc`：

```text
ENV VISIBLE=from_cmdline
RUN |1 SECRET=from_cmdline /bin/sh -c echo "using $SECRET" > /tmp/x # buildkit
ARG SECRET=from_cmdline
CMD ["/bin/sh"]
```

最终镜像 ENV：

```text
["PATH=...","VISIBLE=from_cmdline"]
```

运行时环境：

```text
VISIBLE=from_cmdline
```

结论：

- `ARG SECRET` 不会作为运行时环境变量出现。
- 但它会明文出现在 `docker history` 里。
- `ENV` 会同时出现在镜像元数据和运行时环境。

### 面试答法

`--build-arg` 不能传密码。正确做法是 BuildKit secret mount：

```dockerfile
RUN --mount=type=secret,id=token \
    TOKEN=$(cat /run/secrets/token) && ./download-private-resource "$TOKEN"
```

```bash
docker build --secret id=token,src=./token.txt -t app .
```

secret 只在构建期间挂载，不进入镜像层，也不进入 history。

---

## 七、非 root 用户

容器默认以 root 运行。未启用 user namespace 时，容器里的 root 仍是 UID 0。降低权限的常见写法：

```dockerfile
FROM python:3.12-slim
RUN groupadd -r app && useradd -r -g app app
WORKDIR /app
COPY --chown=app:app app.py .
USER app
CMD ["python", "app.py"]
```

实测：

```text
默认镜像: uid=0(root) gid=0(root)
非 root : uid=999(app) gid=999(app)
```

非 root 写根目录失败：

```text
touch: cannot touch '/test': Permission denied
```

### 反常识发现：新版 Docker 容器内非 root 能绑定 80

实测非 root 用户绑定 80 端口成功：

```text
绑定 80 成功
```

原因不是 capability：

```text
CapPrm: 0000000000000000
CapEff: 0000000000000000
```

而是容器 netns 内的内核参数：

```text
容器内 /proc/sys/net/ipv4/ip_unprivileged_port_start = 0
宿主机 /proc/sys/net/ipv4/ip_unprivileged_port_start = 1024
```

面试时不要机械回答“非 root 一定不能绑定 80”。更准确的说法是：**传统 Linux 默认 1024 以下端口需要特权；但新版 Docker 容器网络命名空间里可能把 `ip_unprivileged_port_start` 设为 0，所以非 root 也能绑定低端口。为了兼容 Kubernetes、旧版本和安全规范，应用仍推荐监听 8080。**

---

## 八、Dockerfile 速查

```dockerfile
FROM golang:1.22-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /app .

FROM alpine:latest
RUN addgroup -S app && adduser -S -G app app
COPY --from=builder --chown=app:app /app /app
USER app
EXPOSE 8080
ENTRYPOINT ["/app"]
CMD ["--port", "8080"]
```

五条实践规则：

1. 安装和清理写在同一条 `RUN`，否则删不掉旧层体积。
2. 依赖清单先 `COPY`，源码后 `COPY`，提高缓存命中率。
3. `CMD`/`ENTRYPOINT` 优先使用 exec 形式。
4. 必须写 `.dockerignore`，至少排除 `.git`、`.env`、私钥、依赖目录和缓存目录。
5. 密钥不要用 `ARG`/`ENV`，用 BuildKit secret。

---

## 九、本次实验修正的既有认知

| 常见说法 | 实测修正 |
|---|---|
| shell 形式 `CMD` 一定让 `/bin/sh` 成为 PID 1 | 不一定。简单命令下 shell 会 exec 自替换；复杂命令才会留下来 |
| 优化 `COPY` 顺序一定明显加速 | 收益取决于依赖安装耗时。无依赖时收益很小，有依赖时可从 2m43s 降到 1.8s |
| `docker stop` 秒回说明应用优雅退出 | 不一定，容器可能早已退出。要结合 `State.Status`、`FinishedAt`、日志判断 |
| 非 root 容器不能绑定 80 端口 | 新版 Docker 可能在容器 netns 内设置 `ip_unprivileged_port_start=0`，非 root 也能绑定 |
| `--build-arg` 不进入运行时环境，所以可传密钥 | 错。它会进入 `docker history`，任何能拉镜像的人都能看到 |
