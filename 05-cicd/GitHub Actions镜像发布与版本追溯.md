# GitHub Actions 镜像发布与版本追溯

> Day 14，2026-10-27，分支 `ci/day14-image-publish`（PR #12，合并提交 `ebf1a24`）。
> Mac 受企业环境限制，不运行本地 Docker；构建、验证与发布都在 GitHub runner 完成。
> 数据来自实际 Actions 运行和 GitHub Packages 页面；未采集或未验证的内容单独标注。

## 一、发布解决什么问题

Day 13 验证过的镜像只存在于 runner，运行结束即消失，无法用于部署。发布把同一份验证过的产物保存到镜像仓库，供后续拉取。

```text
测试 → 构建 → 冒烟验证 → 推送镜像仓库 →（后续）目标机器拉取
```

镜像仓库与另外两类产物不同：

| 类型 | 用途 | 能否 docker pull |
|---|---|---|
| Actions Artifact | 下载检查报告、诊断文件给人看 | 否 |
| Git 仓库 | 版本管理源码与配置 | 否 |
| 镜像仓库 | 保存镜像，支持按版本拉取 | 是 |

本次使用 GitHub Container Registry（GHCR），与 Actions 集成，不需要额外账号或个人 token 写入代码。

## 二、tag 与 digest

| 标识 | 特点 | 本次用途 |
|---|---|---|
| `sha-<commit>` | 便于追溯源码，技术上仍可被重新指向 | 版本命名 |
| `sha256:...` | 内容寻址，固定具体镜像内容 | 精确部署与回滚 |
| `latest` | 可变指针 | 未使用 |

`docker tag` 只是给已有镜像增加名称，不重新构建。发布必须基于刚刚通过冒烟测试的那份镜像，不能在另一个 job 里重新构建一份。

**面试答法：** 用提交 SHA 标签追溯源码，用 digest 固定部署内容。不能只凭 `latest` 判断运行版本——源码相同时，可变基础镜像也可能让两次构建产生不同内容。

## 三、发布权限与触发边界

顶层仍保持：

```yaml
permissions:
  contents: read
```

只在发布相关 job 增加：

```yaml
permissions:
  contents: read
  packages: write
```

登录使用平台提供的 `GITHUB_TOKEN`，通过标准输入传递，不写进代码：

```bash
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
```

### 手动发布开关

```yaml
on:
  workflow_dispatch:
    inputs:
      publish:
        description: "Publish verified image to GHCR (main only)"
        required: true
        type: boolean
        default: false
```

发布步骤同时要求四个条件：

```yaml
if: ${{ success() && github.event_name == 'workflow_dispatch' && inputs.publish && github.ref == 'refs/heads/main' }}
```

| 操作 | 是否发布 |
|---|---|
| push 分支 | 否 |
| 创建 / 更新 PR | 否 |
| 手动运行，未勾 publish | 否 |
| 手动运行非 main，勾了 publish | 否 |
| 手动运行 main、勾 publish、前面验证成功 | 是 |

**实测（push 触发，`1ee7748`）：** `check-markdown` 与 `docker-smoke` 成功，`Log in to GHCR`、`Publish verified image`、`Log out of GHCR` 三个步骤均为 **Skipped**，诊断上传与容器清理正常。这正是期望行为：普通推送不产生发布。

### 权限边界的准确表述

- `packages: write` 是 **job 级权限**，不是只在发布步骤临时授予。
- 为复用已验证的本地镜像，本次让构建、验证、发布处于同一个 job，用条件控制登录与推送。
- **条件判断不是独立的安全审批系统。** 不应在 `pull_request_target` 下执行来自不可信 PR 的代码，否则写权限可能被滥用。

## 四、首次真实发布

手动在 `main` 运行并勾选 publish，运行 `Notes CI #24`：

```text
Status: Success
Total duration: 2m31s
Artifacts: 2
```

步骤结果：

| 步骤 | 结果 |
|---|---|
| Verify HTTP and runtime user | 成功（发布前已验证） |
| Log in to GHCR | 成功 |
| Publish verified image | 成功，8s |
| Log out of GHCR | 成功 |

运行 Summary 生成：

```text
Image tag:     ghcr.io/xuxiaojing-cq/ci-web:sha-ebf1a241c6bfe9914ff10c1b3b687753078acd2c
Source commit: ebf1a241c6bfe9914ff10c1b3b687753078acd2c
```

GitHub Packages 页面实测：

```text
包名：ci-web
可见性：Public
版本：sha-ebf1a241c6bfe9914ff10c1b3b687753078acd2c
digest：sha256:0416a982d2b06591679d9d18d830e09c51815c11b8b69e316db31f1633b8e085
总下载量：0（查看时）
```

页面上显示 **Latest** 标记，但它只是界面标识，**不能据此认定存在名为 `latest` 的镜像标签**。

## 五、发布成功后立刻可用的东西

公开 GHCR 镜像支持匿名拉取，Day 15 在 ECS 上用 digest 拉取验证成功：

```bash
docker pull ghcr.io/xuxiaojing-cq/ci-web@sha256:0416a982d2b06591679d9d18d830e09c51815c11b8b69e316db31f1633b8e085
```

**可见性与可达性是两个问题：** Public 只解决权限，不保证目标机器能访问公网或完整下载镜像层。

## 六、本次未做与后续

- 未做镜像签名、SBOM、来源证明（provenance）。
- 未做多架构构建（本次仅 `linux/amd64`）。
- 未设置镜像保留策略，历史版本会持续累积。
- 未修改可见性，也未为离线环境准备拉取凭据。
- 未实现自动部署与回滚，留待后续练习。

**面试答法：** 发布环节至少要说清三件事：发布的是哪次验证过的产物、用什么标识固定版本、谁有权限写仓库。只说明“镜像推送成功了”是不够的。
