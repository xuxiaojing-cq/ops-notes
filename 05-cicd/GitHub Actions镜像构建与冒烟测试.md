# GitHub Actions 镜像构建与冒烟测试

> Day 13，2026-10-26；分支 `ci/day13-docker-build`。
> Mac 受企业安全策略限制，不运行本地 Docker、不绕过虚拟化限制。Mac 负责编辑、Python 检查和 Git；Docker 构建与运行验证由 GitHub 托管 Ubuntu runner 执行。
> 本次未推送镜像仓库、未部署 ECS、未使用部署密钥。数据来自 Actions 截图及用户实际下载的诊断文件；验证边界明确区分。

## 一、面试主线：测试、构建、运行验证是三层

| 环节 | 回答的问题 | 本次内容 |
|---|---|---|
| 单元测试 | 已知输入下代码行为是否正确？ | 原有 Markdown 检查函数的 5 项测试 |
| 镜像构建 | 程序和运行环境能否打包？ | Python HTTP 服务 Dockerfile |
| 冒烟测试 | 打包后最基本功能是否可用？ | 从 runner 经端口映射请求 `/healthz`，检查响应和运行身份 |

**build 成功、容器 Running 都不代表服务可用。** 冒烟测试需要从实际访问路径验证，而不是只看进程有没有启动。

本次原有 5 项测试不覆盖 HTTP 服务，故障实验里它们通过并不矛盾。成功的冒烟测试也不代表完整业务正确、持续可用或生产性能达标。

## 二、示例服务与 Dockerfile

文件：

- [app.py](../examples/ci-web/app.py)
- [Dockerfile](../examples/ci-web/Dockerfile)
- [.dockerignore](../examples/ci-web/.dockerignore)
- [完整 workflow](../.github/workflows/notes-ci.yml)

服务用 Python 标准库实现：`GET /healthz` 返回 HTTP 200 和 `ok\n`，其他路径返回 404。仅为教学示例，不作为生产 HTTP 服务实现。

Dockerfile：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
```

- `USER` 设非 root UID/GID，后续测试实际检查身份。
- `EXPOSE` 描述端口，不自动发布端口。
- `CMD` 用 exec 形式启动程序；`-u` 使 Python 输出不缓冲，便于诊断。
- 构建上下文是 `examples/ci-web`，不是整个笔记仓库；`.dockerignore` 排除缓存等。
- `python:3.12-slim` 是可变标签，本次没有固定 digest 或验证构建完全可复现。

Mac 本地只做：

```bash
python3 -m py_compile examples/ci-web/app.py
python3 -m unittest discover -s tests -v
python3 scripts/check_notes.py
```

语法检查不证明镜像或 HTTP 服务能运行。Docker CLI 可用但 daemon 不可用，也不能推断打开 Desktop 就能解决企业环境限制。

## 三、两个 job 与 needs

```text
check-markdown → docker-smoke
测试与笔记检查    构建、启动、HTTP/身份验证、诊断与清理
```

```yaml
docker-smoke:
  needs: check-markdown
  runs-on: ubuntu-24.04
  timeout-minutes: 10
```

`needs` 让前置 job 成功后才执行后续 job；默认前置失败会使依赖 job 跳过。不同 job 在本次托管环境中使用独立 runner，都需要 checkout，不能直接依赖另一个 job 的文件或 Docker 镜像。

### 构建与启动

```bash
docker build -t ci-web:${{ github.sha }} examples/ci-web

docker run -d \
  --name day13-ci-web \
  -p 127.0.0.1:18080:8080 \
  ci-web:${{ github.sha }}
```

这里的 Docker 命令在 GitHub runner 上执行，不在 Mac 或 ECS 上执行。

- runner 的回环端口 18080 映射到容器端口 8080。
- 镜像标签使用本次运行的 `github.sha` 便于追溯；PR 事件中它可能对应合并候选提交，而不是分支头提交。
- 标签不是镜像内容 digest；同一源码配合可变基础镜像仍可能生成不同内容。
- 本次镜像只用于当前 runner 验证，不推送仓库、不作为 Artifact 保存。

## 四、HTTP 和身份验证

实际检查用有限重试：最多 15 次，每次 curl 超时 2 秒，失败后睡眠 2 秒。不是保证固定 30 秒窗口，请求本身耗时也计入。

关键逻辑：

```bash
curl --fail --show-error --silent --max-time 2 \
  http://127.0.0.1:18080/healthz -o health-response.txt
```

请求成功后还比较正文是否为 `ok`，不是任意 2xx 就算通过；这里 Shell 命令替换会去除末尾换行。15 次尝试耗尽则主动 `exit 1`，不把持续失败当成可忽略现象。

身份检查：

```bash
docker exec day13-ci-web id
test "$(docker exec day13-ci-web id -u)" = "10001"
test "$(docker exec day13-ci-web id -g)" = "10001"
```

打印值方便读日志，`test` 让不符合预期的身份真正造成失败。HTTP 检查失败时后面的身份检查不会执行，不能把故障版本的身份写成已验证。

## 五、实测：成功、故障、恢复

### 首次成功：89b850e

```text
check-markdown：成功，6s
docker-smoke：成功，17s
Build image：成功，步骤显示 9s
Health check attempt 1：curl (56) Connection reset by peer
Health check attempt 2：返回 ok
Runtime user：uid=10001 gid=10001 groups=10001
```

Summary 显示两个 job 的依赖关系，产物 `docker-smoke-diagnostics` 和 `notes-check-report` 均存在，用户确认日志可以下载。

第一次请求失败、第二次成功与启动瞬间的时序问题相符，但**具体首次连接重置原因未定位**，不能写成已确认根因。

### 故意失败：3bf5947

仅把服务绑定地址从：

```python
HTTPServer(("0.0.0.0", 8080), Handler)
```

改为：

```python
HTTPServer(("127.0.0.1", 8080), Handler)
```

实测：

```text
check-markdown：成功
docker-smoke：失败，44s
HTTP 验证步骤：30s，尝试到第 15 次
curl (56) Recv failure: Connection reset by peer
ERROR: health check did not return ok
Process completed with exit code 1
收集诊断：成功
上传诊断：成功
```

用户查看下载日志确认容器为 Up，程序输出监听 8080。说明程序未因这次改动退出，但 runner 经映射端口无法访问服务。

### 根因：端口正确，监听地址错误

```text
runner 127.0.0.1:18080 → 容器网卡:8080
                         不等于容器回环 127.0.0.1:8080
```

容器内绑定 `127.0.0.1` 只接收自身回环请求，端口映射不会自动修改应用绑定地址；`0.0.0.0` 才覆盖容器 IPv4 网卡。

日志 `Listening on port 8080` 没有包含绑定地址，也不是系统 socket 状态采集；不能仅凭这句话判断外部可达。结合有意修改的代码、持续失败与恢复后的成功，才能建立实验根因证据链。

### 恢复：3793c10

改回 `0.0.0.0` 后：

```text
check-markdown：成功
docker-smoke：成功，17s
Build image：成功，7s
第一次 HTTP 请求连接重置，第二次返回 ok
uid=10001 gid=10001 groups=10001
收集、上传、清理步骤均成功
```

验证范围：基本 HTTP 功能及运行身份恢复，不证明全过程零中断或所有业务路径正常。

## 六、诊断产物与清理

workflow 在失败后使用 `if: ${{ !cancelled() }}` 尝试采集和上传：

| 文件 | 内容 |
|---|---|
| `containers.txt` | Docker 容器状态 |
| `container.log` | 应用标准输出和错误日志 |
| `inspect.json` | 容器配置、状态、端口等详情 |
| `health-response.txt` | 若已生成则保存请求正文；失败时不保证包含有效响应 |

Artifact 名为 `docker-smoke-diagnostics`，配置保留 7 天。保存的是诊断文件，不是镜像。inspect 等日志可能含环境变量或敏感信息，实际部署项目需脱敏或限制访问。

诊断与清理里的 `|| true` 允许容器不存在等情况，不用于核心 build、run 或健康检查，因此不会把核心失败改绿。但它也意味着诊断命令可能未成功，仍需读 Artifact 内容，不只看上传步骤绿勾。

最后清理：

```yaml
- name: Remove test container
  if: ${{ always() }}
  run: docker rm -f day13-ci-web || true
```

用于成功、失败或取消时尝试移除测试容器；runner 强制终止等情况下不能保证执行。恢复截图显示清理步骤成功，但未再次查询 Docker 列表独立证明容器删除。

两个 job 都收到 upload-artifact@v4 的 Node.js 20 弃用提醒，实际上传未受阻。本次未升级该 Action，不把警告当成本次 HTTP 失败原因。

## 七、合并门槛与面试速记

`needs` 管执行顺序；ruleset 管合并条件，二者不是同一件事。

后续 PR 收尾应保留 `check-markdown`，再将 `docker-smoke` 加入 main 的必需检查，观察 PR 上 Required 标记。本次只完成 CI 实验，**尚未验证 docker-smoke 已成为必需检查**。

面试答法：

> 镜像 build 成功和容器 Running 只证明打包和启动阶段通过，还要从实际访问路径执行冒烟测试。本次服务误绑定容器内回环，程序正常运行但端口映射后的请求持续失败，改为 0.0.0.0 后恢复。CI 使用有限重试、非零退出码和失败诊断 Artifact，既容忍启动瞬间失败，也不掩盖持续故障。

今日完成：CI 镜像构建、HTTP/非 root 身份验证、失败诊断与恢复。未完成：镜像发布、生产部署、跨架构验证、应用完整测试和生产容量评估。
