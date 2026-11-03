# 从 CI 自动部署到 ECS：Ansible + 审批闸门

> Day 18，2026-11-03。分支 `ci/day18-deploy`（PR #17，合并提交 `7278c0d`）。
> 控制节点为 GitHub 托管 runner，目标机为 ECS；Mac 只做编辑与 Git。
> 数据来自实际 Actions 运行、ECS 终端输出和只读核实；未验证项明确标注。

## 一、目标与链路

把 Day 17 的手工 Ansible 部署，接成「CI 验证 → 发布镜像 → 人工批准 → 自动部署」。

```text
手动触发（main + publish + deploy）
   ↓
check-markdown   测试 + 笔记检查
   ↓ needs
docker-smoke     构建 + 冒烟 + 推送 GHCR
   ↓ outputs.image_ref（digest 引用）
deploy           人工审批 → SSH + Ansible → 部署 → 验证
```

实测总耗时 **3m29s**，其中 deploy 2m36s（主要花在装 Ansible 与拉镜像）。第二次运行 1m10s，因为基础镜像已缓存。

## 二、先接受风险：这一步在给什么权限

```text
公网 runner → 部署私钥 → ECS 的 root 账号 → docker 命令（等同 root）
```

必须正视的事实：

| 风险 | 说明 |
|---|---|
| Secrets 谁能读 | 仓库管理员、能改 workflow 的人（可让流水线打印出来） |
| 有效期 | 长期有效，不像 `GITHUB_TOKEN` 会过期 |
| 泄露后果 | 可直接 root 登录服务器 |
| 日志掩码 | 自动掩码存在，但写入文件或变形输出后可能失效，不能依赖它兜底 |

对应的缓解措施：

1. **专用密钥**，不复用日常 SSH 密钥
2. **`production` 环境 + Required reviewers**，部署前必须人工批准
3. 只在 **手动触发 + main + 两个复选框都勾** 时执行
4. 部署结束**销毁临时目录中的私钥文件**

⚠️ **无法用 `from=` 限制来源 IP**：GitHub 托管 runner 出口 IP 是动态的，硬编码会锁死流水线。所以实际取舍是放宽来源限制，改用"专用密钥 + 审批闸门"控制风险。

⚠️ **不代表客户环境可照搬**：真实客户服务器通常不对公网开放 22 端口。那种情况要在客户内网部署 **self-hosted runner**，或经堡垒机跳转。

## 三、密钥准备（在 Mac 完成）

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gha_deploy_ed25519 -C "github-actions-deploy" -N ""
```

- `-N ""`：无口令，因为 Actions 中无人交互输入
- 公钥追加到 ECS 的 `~/.ssh/authorized_keys`

验证必须排除"其实用了别的密钥"：

```bash
ssh -i ~/.ssh/gha_deploy_ed25519 -o IdentitiesOnly=yes root@<HOST> "echo ok && hostname"
```

`IdentitiesOnly=yes` 强制只用指定密钥。同时核实公钥在 `authorized_keys` 中**只出现 1 次**。

### 主机指纹

```bash
ssh-keyscan -H <HOST> > /tmp/ecs_known_hosts
```

存入 Secret 后在 playbook 侧强制校验：

```text
-o UserKnownHostsFile=$WORK_DIR/known_hosts -o StrictHostKeyChecking=yes
```

**不使用运行时 `ssh-keyscan`**——那等于"第一次见谁就信谁"，起不到防护作用。

## 四、Secrets 与审批环境

| Secret | 内容 |
|---|---|
| `ECS_HOST` | 服务器地址 |
| `ECS_USER` | 登录用户 |
| `ECS_SSH_KEY` | 私钥全文（用 `pbcopy` 送入，避免手工复制漏换行） |
| `ECS_KNOWN_HOSTS` | `ssh-keyscan` 输出 |

环境：`Settings → Environments → production`，勾选 Required reviewers 并加入自己。

**单人仓库里审批人也是自己，看似形式化，但实际作用是把自动化变成"必须有人主动确认"并留下批准记录。**

## 五、workflow 的三个关键设计

### 1. 用 job output 传递 digest，而不是 tag

```yaml
# docker-smoke
outputs:
  image_ref: ${{ steps.publish.outputs.image_ref }}
```

发布步骤从 push 日志解析 digest：

```bash
digest="$(grep -oE 'digest: sha256:[0-9a-f]{64}' reports/docker/push.log | tail -1 | awk '{print $2}')"
if [ -z "$digest" ]; then
  echo "ERROR: could not read digest from push output" >&2
  exit 1
fi
echo "image_ref=ghcr.io/${owner}/ci-web@${digest}" >> "$GITHUB_OUTPUT"
```

**解析不到就失败**，不允许带着空值继续。

### 2. deploy job 的权限和闸门

```yaml
deploy:
  needs: docker-smoke
  if: ${{ success() && github.event_name == 'workflow_dispatch' && inputs.deploy && inputs.publish && github.ref == 'refs/heads/main' }}
  environment: production
  permissions:
    contents: read
```

- 不需要 `packages: write`，它只读仓库和拉镜像
- 必须同时勾 publish，否则拿不到镜像地址

### 3. 临时凭据与清理

```yaml
- name: Prepare SSH access and inventory
  env:
    WORK_DIR: ${{ runner.temp }}/ansible-deploy
  run: |
    install -d -m 700 "$WORK_DIR"
    printf '%s\n' "$ECS_SSH_KEY" > "$WORK_DIR/deploy_key"
    chmod 600 "$WORK_DIR/deploy_key"
```

结束清理：

```yaml
- name: Remove deployment credentials
  if: ${{ always() }}
  run: |
    shred -u "$WORK_DIR/deploy_key" 2>/dev/null || rm -f "$WORK_DIR/deploy_key"
    rm -rf "$WORK_DIR"
```

`always()` 保证部署失败也清理。**这不是绝对安全**——runner 本身是临时机器，整体销毁才是最终保障；这里做的是不把明文密钥留在磁盘上。

部署参数用 `-e` 覆盖 playbook 默认值，落地到独立端口：

```bash
ansible-playbook -i "$WORK_DIR/inventory.ini" 05-cicd/ansible/deploy-ci-web.yml \
  -e "app_name=gha-ci-web" -e "app_port=18084" -e "app_image=$IMAGE_REF"
```

## 六、实测结果

### 运行 #39（首次部署）

```text
check-markdown   7s   ✅
docker-smoke    20s   ✅
deploy       2m36s   ✅
Playwright 总时长 3m29s
```

Summary 输出：

```text
Image tag:         ghcr.io/<owner>/ci-web:sha-7278c0dbaf...
Image digest ref:  ghcr.io/<owner>/ci-web@sha256:24d9bf0ec...
```

审批记录：页面顶部显示 `The deployments have been approved.`，**证明人工闸门实际生效**。

### ECS 侧核实

```text
gha-ci-web   Up   127.0.0.1:18084->8080/tcp
/healthz     ok
运行身份      uid=10001 gid=10001
```

三个容器并存，互不干扰：

| 容器 | 端口 | 来源 |
|---|---|---|
| `day15-ci-web` | 18080 | Day 15 手工部署 |
| `ansible-ci-web` | 18083 | Day 17 手工 Ansible |
| `gha-ci-web` | 18084 | Day 18 Actions 自动部署 |

### 运行 #40（第二次部署）：发现"重建"

第二次运行后容器创建时间变为新的时间戳，说明**容器被重建**。

原因不是 playbook 逻辑错误，而是**输入变了**：

```text
#39 构建 → 镜像 24d9bf0e...
#40 构建 → 镜像 1bda30a5...   ← 每次 build 都可能产出新 digest
```

基础镜像 `python:3.12-slim` 是可变标签，上游更新即产生不同内容 → digest 变化 → playbook 判断"版本不一致" → 删除重建。

## 七、构建幂等与部署幂等是两件事

| 层面 | 是否幂等 | 依据 |
|---|---|---|
| Ansible 部署 | ✅ | 同一 digest 连续两次 `changed=0`，容器未重建 |
| CI 构建 | ❌ | 每次 `docker build` 可能产出新 digest |

### 实测：固定 digest 连跑两次

```bash
DIGEST='ghcr.io/<owner>/ci-web@sha256:1bda30a5...'

ansible-playbook deploy-ci-web.yml \
  -e "app_name=gha-ci-web" -e "app_port=18084" -e "app_image=$DIGEST"
```

结果：

```text
第一次: web01 : ok=11  changed=0  skipped=3
第二次: web01 : ok=11  changed=0  skipped=3

容器 Created = 2026-11-03T14:33:19Z   ← 两次执行前后不变
Restarts = 0
/healthz = ok
```

**面试答法：** 部署幂等性取决于输入是否稳定。给定同一镜像 digest，Ansible 是幂等的；但若上游构建每次都产出新 digest（可变基础镜像、构建时间戳等），部署层就会判定版本变化而重建。要让"源码未变就不重启"成立，必须让构建也可复现——固定基础镜像 digest、避免写入构建时间。

固定基础镜像 digest 是取舍：安全补丁不会自动获取，需要定期手动更新。

## 八、本次踩到的两个 workflow 问题

### 1. `runner` 上下文不能用在 job 级 env

```text
Unrecognized named-value: 'runner'. Located at position 1 within expression: runner.temp
```

job 级 `env` 允许的上下文有限，`runner` 只在 step 级可用。修复：把 `WORK_DIR` 下移到各个 step 的 `env`。

⚠️ 相关陷阱：env 的值**不会展开内部的 shell 变量**，写 `$HOME/...` 会变成字面量。

### 2. 分支没加进 `on.push.branches`

推送后完全没有新的运行记录。原因是分支列表是**逐个列出**的，新分支不在其中。

有意思的是：**第一次推送时出现了运行**，但那次是 `Invalid workflow file` 解析失败——解析失败时 GitHub 直接把错误显示成一次失败运行，分支过滤还没轮到生效。修好之后过滤正常，于是"没新记录"。

⭐ 排查思路：**"没触发"和"配置错了"要分开看。** 完全没有记录通常意味着触发条件不匹配；记录存在但失败，才是内容有问题。

## 九、收尾：容器与密钥

三个学习容器仅用于验证，**未能持续运行的价值**，收尾时可停止（保留镜像与记录）：

```bash
docker stop day15-ci-web ansible-ci-web gha-ci-web
```

**部署密钥是另一件事。** 它仍能直接 root 登录服务器，且没有 `from=` 限制。如果不再需要 CI 部署，应主动撤销：

```bash
sed -i '/github-actions-deploy/d' ~/.ssh/authorized_keys
```

并在 GitHub 上删除四个 Secrets 与 `production` 环境。**只删私钥、不删公钥，或只删公钥、不删私钥，都不算撤销**——两边都要清。

## 十、本次未覆盖

- 部署失败时自动回滚（Day 16 是人工回滚）
- 多主机/多机构并行部署与 `serial` 分批
- `block`/`rescue` 失败处理与部分主机失败策略
- 用 Vault 或外部密钥管理替代明文 Secret
- self-hosted runner 方案（客户内网不可达场景）
- 部署后的持续可用性观察窗口与自动判定
