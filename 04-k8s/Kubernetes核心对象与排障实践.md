# Kubernetes 核心对象与排障实践

> Day 10：单节点 k3s 实验，2026-10-20。
> 环境采集结果：Ubuntu 22.04.5 LTS、2 核 CPU、约 1.6 GiB 内存；k3s / kubectl v1.36.5+k3s1，运行时 containerd。
> 内容面向交付运维面试。实验结果来自实际终端输出；未验证的结论明确标注，不用预期结果代替实测。
> 输出中的节点名统一替换为 `<node>`，不包含公网地址、凭据或公司业务信息。

---

## 一、核心心智模型：声明式与持续控制

Compose 同样使用声明式配置，但 `up` 主要是一次部署协调；之后的容器退出恢复由 Docker restart 策略处理。Kubernetes 的控制器则持续协调集群对象的期望状态与实际状态。

```text
提交 YAML → API Server → 保存集群状态
                            ↓
                     控制器持续协调
                            ↓
                创建、更新、删除资源以收敛
```

| 组件 | 主要职责 |
|---|---|
| API Server | 集群 API 入口，认证、授权、校验与状态读写 |
| etcd | 标准 Kubernetes 常用的状态存储；单服务器 k3s 默认可使用 SQLite，不应把它说成一定有 etcd |
| Scheduler | 给尚未调度的 Pod 选择节点 |
| Controller Manager | 运行副本、节点等控制器 |
| kubelet | 管理本节点上的 Pod 和容器、执行探针、报告状态 |
| kube-proxy | 常见实现中维护 Service 转发规则；部分网络方案可替代它 |
| containerd | 实际创建、运行和停止容器 |

控制组件通过 API Server 交换集群状态，但不能说“所有组件只与 API Server 通信”：kubelet 也调用容器运行时，探针会访问应用，数据流量也不经过 API Server。

### 面试答法

> Kubernetes 的核心是持续协调。用户声明期望状态，控制器观察实际状态并驱动收敛。ReplicaSet 维持副本数，Deployment 管理模板版本与滚动更新，kubelet 负责节点内的容器生命周期。

节点故障时，有控制器管理的 Pod 可以在其他合适节点补建，但不是立即、无条件恢复。本次只有一个节点，**没有验证跨节点故障恢复**。

---

## 二、Pod：容器重启与 Pod 重建不是一回事

Pod 是最小调度单位，可以包含多个容器。

- 同 Pod 的常规容器共享网络命名空间：同一个 Pod IP，可以通过 `localhost` 通信，端口也可能冲突。
- Pod 可以定义共享卷，各容器通过 `volumeMounts` 显式挂载；不是所有文件系统自动共享。
- 容器重启可以发生在原 Pod 内，Pod UID 不变。
- 删除 Pod 后再创建的是新对象；裸 Pod 没有 ReplicaSet 帮它补建。

### 实测：创建与启动

```bash
kubectl run my-nginx --image=nginx:alpine --port=80
kubectl get pods -o wide
kubectl describe pod my-nginx
kubectl logs my-nginx
kubectl exec -it my-nginx -- sh
```

实际 Events：

```text
Scheduled  Successfully assigned default/my-nginx to <node>
Pulling    Pulling image "nginx:alpine"
Pulled     Successfully pulled image in 34.06s. Image size: 28826677 bytes.
Created    Container created
Started    Container started
```

这条顺序对应：调度 → 拉镜像 → 创建容器 → 启动进程。排障时从 Events 判断卡在哪一层。

### Pod YAML 示例

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: demo-pod
  labels:
    app: demo
spec:
  containers:
    - name: web
      image: nginx:alpine
      ports:
        - containerPort: 80
      resources:
        requests:
          memory: "64Mi"
          cpu: "100m"
        limits:
          memory: "128Mi"
          cpu: "200m"
```

`containerPort` 描述容器端口，不会自动创建 Service，也不会把端口发布到公网。

### 实测：原 Pod 内的容器重启

```bash
kubectl exec demo-pod -- sh -c 'kill 1'
kubectl get pod demo-pod -w
```

```text
操作前：demo-pod   1/1 Running   RESTARTS=0   IP=10.42.0.4
操作后：demo-pod   1/1 Running   RESTARTS=1
```

实测证明了容器重启；操作后的 IP / UID 未再次采集，不把它们当作本次直接对照证据。`kill 1` 在这个 nginx 示例中有效，不代表任意镜像中的 PID 1 都会同样处理 SIGTERM。

### requests 与 limits

| 配置 | 影响 |
|---|---|
| CPU / memory requests | 调度容量分配依据，不是实时用量 |
| CPU limits | 常见配置下限制 CPU 使用，超限被节流，不是直接杀进程 |
| memory limits | 超过限制可能触发容器 OOM |

`100m` 表示 0.1 个 CPU 核心。设置 requests / limits 需要结合业务测量；尤其 CPU limit 可能增加延迟，不能把“所有服务一律设置同样限制”当生产规范。

---

## 三、Deployment：副本、自愈、滚动更新与回滚

```text
Deployment → ReplicaSet → Pod → 容器
    │             │
    │             └─ 维持副本数
    └─ 管理 Pod 模板版本、滚动更新
```

关键 YAML 字段：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-deploy
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
        - name: nginx
          image: nginx:alpine
          ports:
            - containerPort: 80
          resources:
            requests:
              memory: "32Mi"
              cpu: "50m"
            limits:
              memory: "64Mi"
              cpu: "100m"
```

selector 必须匹配模板标签；不要让不同工作负载的 selector 意外重叠。

### 实测：删除一个 Pod 后补齐

删除 `nginx-deploy-7c7875bfc9-4mm77` 后：

```text
nginx-deploy-7c7875bfc9-dnnfp   1/1 Running   AGE=2m15s
nginx-deploy-7c7875bfc9-dx49g   1/1 Running   AGE=3s
nginx-deploy-7c7875bfc9-lqt6w   1/1 Running   AGE=2m15s
```

新名字和 AGE=3s 证明补建了新 Pod，副本数回到 3。不是把被删除的 Pod 重新 start。

### 扩缩容与配置文件漂移

```bash
kubectl scale deployment nginx-deploy --replicas=5
kubectl scale deployment nginx-deploy --replicas=2
```

命令修改的是在线对象，不会改本地 YAML。若文件仍写 `replicas: 3`，重新 apply 可能把在线副本数恢复为 3。生产应明确配置的唯一来源。

缩容不是简单“随机删除”；控制器有 Pod 选择逻辑。学习时只需检查最终副本数，不依赖某个 Pod 必然被保留。

### 实测：滚动更新与回滚

```bash
kubectl set image deployment/nginx-deploy nginx=nginx:1.25
kubectl rollout status deployment/nginx-deploy
kubectl get rs
kubectl rollout undo deployment/nginx-deploy
kubectl get rs
```

| ReplicaSet | 更新完成 DESIRED / CURRENT / READY | 回滚后 |
|---|---|---|
| `nginx-deploy-566997dc6d` | 2 / 2 / 2 | 0 / 0 / 0 |
| `nginx-deploy-7c7875bfc9` | 0 / 0 / 0 | 2 / 2 / 2 |

回滚将 Pod 模板恢复到上一修订，旧 ReplicaSet 再次扩容。保留历史受 `revisionHistoryLimit` 等配置影响，不是永久保存所有版本。

**滚动更新不自动保证零中断**：还需要 readiness、合理的 `maxSurge` / `maxUnavailable`、优雅终止、资源容量以及应用兼容性。本次验证了副本切换，没有做持续可用性测试。`nginx:1.25` 仅用于版本切换练习，不是生产版本推荐。

### 回滚 Warning 的正确解释

终端提示 undo 不更新 `last-applied-configuration`，可能影响后续 apply。实际查询：

```text
last-applied 中的 image：nginx:alpine
实际模板中的 image：nginx:alpine
```

操作顺序是 `apply alpine → set image 1.25 → undo alpine`。两者一致可以由“回到了最初 apply 的镜像”解释，**不能证明新版 kubectl 在 undo 时同步更新了注解**。此前关于版本行为的推断撤回。

这里只比较了镜像字段，不等于整个注解与在线对象完全一致；例如副本数可能已被 scale 改动。

面试中强调：命令式修改不会自动更新本地声明文件。使用 Git 管理 YAML 时，回滚后应让版本库与在线期望状态保持一致；server-side apply 使用字段管理，也不是自动解决所有漂移。

---

## 四、三种探针：重启、就绪、启动保护

| 探针 | 核心问题 | 连续失败达到阈值后的效果 |
|---|---|---|
| livenessProbe | 是否需要重启恢复？ | kubelet 终止容器，再按重启策略处理 |
| readinessProbe | 是否能接业务流量？ | 标记不就绪，默认 Service 不向该后端转发正常流量；不因此重启 |
| startupProbe | 启动是否完成？ | 成功前抑制 liveness / readiness；持续失败则终止容器并按策略处理 |

检查方式包括 HTTP、TCP、exec 和 gRPC。HTTP 探针通常将 200–399 视为成功；TCP 只能证明连接可建立，不能证明业务功能正常。

Docker healthcheck 失败主要标记 `unhealthy`，本身不触发 Docker restart 策略。Kubernetes liveness 失败则有主动终止容器的行为。

### 实验使用的探针配置

readiness 实验使用 3 副本 nginx Deployment：

```yaml
readinessProbe:
  httpGet:
    path: /
    port: 80
  initialDelaySeconds: 5
  periodSeconds: 3
  failureThreshold: 2
```

liveness 实验是裸 Pod `live-demo`，用相同路径与周期：

```yaml
livenessProbe:
  httpGet:
    path: /
    port: 80
  initialDelaySeconds: 5
  periodSeconds: 3
  failureThreshold: 2
```

### 实测：readiness 失败不重启

```bash
POD=$(kubectl get pods -l app=probe-demo -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$POD" -- sh -c 'rm /usr/share/nginx/html/index.html'
```

```text
probe-demo-5db8b4dd8c-flnsq   0/1 Running   RESTARTS=0
probe-demo-5db8b4dd8c-g5w6z   1/1 Running   RESTARTS=0
probe-demo-5db8b4dd8c-xxwbz   1/1 Running   RESTARTS=0

probe-svc 的就绪 Endpoints：10.42.0.18:80,10.42.0.19:80
```

容器仍运行，但就绪后端从三个减少为两个。删除首页造成了非成功 HTTP 响应；未采集探针响应码，不能写成一定是 404。

### 实测：liveness 失败后重启

```bash
kubectl exec live-demo -- sh -c 'rm /usr/share/nginx/html/index.html'
kubectl get pod live-demo -w
```

```text
破坏前：live-demo   1/1 Running   RESTARTS=0
破坏后：live-demo   1/1 Running   RESTARTS=1
```

重启的是原 Pod 内的容器。首页删除发生在容器可写层，新容器从镜像重新创建后文件恢复；若破坏的是持久化卷内容，重启未必能修复。

没有采集连续探针时间线，不能承诺固定 6–10 秒重启；终止宽限期等也会影响恢复时间。

### 实测：startupProbe 快速通过

```yaml
startupProbe:
  httpGet:
    path: /
    port: 80
  failureThreshold: 30
  periodSeconds: 2
```

```text
AGE=0s   0/1 ContainerCreating   RESTARTS=0
AGE=1s   0/1 Running             RESTARTS=0
AGE=3s   1/1 Running             RESTARTS=0
```

本次证明配置下快速启动成功，没有验证慢启动保护对照。`30 × 2 ≈ 60 秒` 是启动探测容忍窗口的常用估算，不是精确秒表保证。

### 面试答法

> readiness 失败先摘流量，不重启；liveness 失败用于重启恢复；startup 给慢启动服务独立容忍窗口。liveness 不宜把所有外部依赖都纳入，否则数据库短暂故障可能导致所有应用反复重启。

---

## 五、Service：Pod 换了，入口不变

实验 Service：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: probe-svc
spec:
  selector:
    app: probe-demo
  ports:
    - port: 80
      targetPort: 80
```

默认类型是 ClusterIP。selector 匹配 Pod，EndpointSlice 跟踪后端地址及就绪条件。ClusterIP 是虚拟服务地址，不要求宿主机网卡上直接绑定这个 IP。

### 完整实测对照

| 对比项 | 删除前 | 补建并就绪后 |
|---|---|---|
| Service ClusterIP | `10.43.150.38` | `10.43.150.38` |
| 替换的 Pod | `flnsq`，IP `10.42.0.17`，0/1 | `vpxg8`，IP `10.42.0.24`，1/1，AGE=23s |
| 其他两个 Pod | `g5w6z`、`xxwbz` | 保留 |
| 通过原 Service IP 请求 | HTTP 200 | HTTP 200 |

```bash
SVC_IP=$(kubectl get svc probe-svc -o jsonpath='{.spec.clusterIP}')
curl --max-time 5 -I "http://$SVC_IP"

kubectl delete pod probe-demo-5db8b4dd8c-flnsq
kubectl wait --for=condition=Ready pod -l app=probe-demo --timeout=120s
kubectl get pods -l app=probe-demo -o wide
kubectl get svc probe-svc
curl --max-time 5 -I "http://$SVC_IP"
```

两次实际响应：`HTTP/1.1 200 OK`，Server 为 `nginx/1.31.7`。

结论：后端 Pod 被替换、IP 改变，Service 入口保持不变，替换前后两次采样请求均成功。**不是全过程零中断证明**。删除前仍有两个就绪后端，首次访问成功符合预期。

### EndpointSlice 为什么还显示未就绪 IP

```text
kubectl get endpointslices ... -o wide
ENDPOINTS: 10.42.0.17,10.42.0.18,10.42.0.19
```

wide 输出显示地址，不显示各地址的 readiness。地址存在不等于正在接流量；应检查：

```bash
kubectl get endpointslices \
  -l kubernetes.io/service-name=probe-svc -o yaml
```

关注 `endpoints[].conditions.ready`。本次未采集该条件字段，不能将 `ready: false` 写成直接观测输出。

本环境提示旧 `v1 Endpoints` 自 v1.33 起弃用，后续优先使用 EndpointSlice。

### 访问方式速记

- ClusterIP：集群内部服务入口；本实验从 ECS 节点访问成功，不是 Mac 公网访问。
- NodePort：在节点端口提供访问，仍需考虑路由、防火墙、安全组。
- LoadBalancer：依赖负载均衡实现，不是写完 YAML 就必然有公网 IP。
- `kubectl port-forward`：临时调试，默认绑定本地回环；在 ECS 上运行时，这个“本地”是 ECS。没有 `-d` 后台选项，可用另一个终端测试。

---

## 六、排障实战：状态是线索，不是根因

统一顺序：

```text
get 状态 → describe / Events → logs / 终止信息 → 定位层次 → 修复 → 验证
```

`CrashLoopBackOff`、`ImagePullBackOff` 是 kubectl 展示的容器状态线索，不是独立的 Pod phase。Pending 也可能包含等待拉镜像等阶段，不能一看到 Pending 就断言 CPU 不足。

### 1. CrashLoopBackOff：启动命令退出码 1

```bash
kubectl run crash-demo --image=nginx:alpine --restart=Always \
  --command -- sh -c 'echo "demo startup failed"; exit 1'
kubectl get pod crash-demo -w
kubectl describe pod crash-demo
kubectl logs crash-demo
kubectl logs crash-demo --previous
kubectl get pod crash-demo -o jsonpath='{.status.containerStatuses[0].lastState.terminated}'
```

实测：

```text
Error → CrashLoopBackOff → 短暂 Running → Error
RESTARTS：1 → 2 → 3
Events：Back-off restarting failed container
当前日志：demo startup failed
lastState.terminated：exitCode=1，reason=Error
```

`logs --previous` 实际失败：

```text
unable to retrieve container logs for containerd://...
```

具体原因未查明，不推定为日志清理或运行时故障。本次当前日志和退出信息足以证明：启动命令主动失败，kubelet 反复重启并退避。

短暂 `1/1 Running` 不说明业务健康：没有 readinessProbe，进程刚启动就可能被标记就绪，随后马上退出。

面试排查方向：配置 / 命令错误、依赖连接失败、OOM、探针误杀。结合 `lastState`、Events 和日志区分，不能只凭 137 判断就是内存超限。

### 2. ImagePullBackOff：代理返回 403

```bash
kubectl run image-demo --image=nginx:day10-no-such-tag-20261020
kubectl describe pod image-demo
```

```text
ContainerCreating → ErrImagePull → ImagePullBackOff
失败请求：镜像代理 docker.m.daocloud.io 的 manifest HEAD 请求
实际返回：403 Forbidden，denied
```

虽故意设置了不存在的标签，实际证据是代理拒绝请求，**没有直接验证上游返回 manifest unknown**；也不能据此判断用户 Docker Hub 账号权限有问题。

修复：

```bash
kubectl set image pod/image-demo image-demo=nginx:alpine
kubectl wait --for=condition=Ready pod/image-demo --timeout=120s
```

```text
pod/image-demo condition met
image-demo   1/1 Running   RESTARTS=0
```

已知镜像此前有缓存，恢复不等于证明代理网络 / 权限已修复。

| Events 原始错误 | 排查方向 |
|---|---|
| not found / manifest unknown | 仓库名、标签、镜像是否存在 |
| unauthorized / denied / 403 | 凭据、仓库权限、代理访问策略 |
| DNS / timeout / TLS | 网络、DNS、证书、镜像源 |

### 3. Pending：请求 3 核，节点仅 2 核

实验 Pod 请求 CPU `"3"`，调度失败，因此不会实际消耗三个核心。

```text
FailedScheduling:
0/1 nodes are available: 1 Insufficient cpu.
Preemption is not helpful for scheduling.
```

即使抢占其他 Pod，也无法让这个 2 核节点容纳 3 核请求。

```bash
kubectl describe pod pending-demo
kubectl describe node
```

看节点的 `Allocatable`、`Allocated resources`，调度按 requests 做容量判断，不按 `top` 的实时空闲比例。

其他调度约束包括污点 / 容忍、节点亲和性、Pod 亲和性、PVC 等；以 `FailedScheduling` 的实际错误缩小范围。

---

## 七、面试速查与验证边界

| 问题 | 简要回答 |
|---|---|
| Pod 与容器是什么关系？ | Pod 是调度单元，可包含多个容器，常规容器共享网络，卷通过显式挂载共享 |
| 容器重启会产生新 Pod 吗？ | 不一定；kubelet 可在原 Pod 内重启容器，删 Pod 后补建才是新对象 |
| 谁负责副本数，谁负责更新？ | ReplicaSet 维持副本数，Deployment 管理模板版本与滚动更新 |
| Pod IP 会变，怎么访问？ | 用 Service 的稳定地址 / DNS，通过 selector 与 EndpointSlice 跟踪后端 |
| readiness 和 liveness 区别？ | 前者控制就绪与流量，后者用于终止并恢复不健康容器 |
| CPU 很闲为什么 Pending？ | 调度看 requests 与可分配容量，还要检查其他调度约束 |
| CrashLoopBackOff 怎么查？ | describe 的退出原因 / Events，再看当前和 previous 日志 |
| ImagePullBackOff 怎么查？ | 看原始拉取错误，不仅看状态名 |
| 滚动更新保证无损吗？ | 不自动保证；探针、容量、终止过程和应用兼容性都重要 |

本次未验证：多节点故障迁移、慢启动保护对照、持续请求下的零中断、EndpointSlice readiness 条件原文、previous 日志失败根因。

学习机添加 swap 是内存压力后备，不是扩充物理内存或保证不会 OOM；kubelet 的 swap 支持与配置有关，也不能说容器必然可用该 swap。安装精简 k3s 属于资源受限实验，不作为生产容量建议。

故障实验清理命令：

```bash
kubectl delete pod crash-demo image-demo pending-demo --ignore-not-found
```

正常示例先保留用于后续学习；不在本次整理笔记时修改服务器服务或自动更新配置。
