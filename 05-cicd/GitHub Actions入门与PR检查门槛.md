# GitHub Actions 入门与 PR 检查门槛

> Day 11，2026-10-21。以 ops-notes 仓库的真实实验为例。
> 范围：Git 操作主线、Actions 基础、失败与修复、必需检查。尚未涉及构建或自动部署。

## 一、Git 操作：本地改动如何进入 GitHub

```text
工作区 → git add → 暂存区 → git commit → 本地提交
                                         ↓ git push
                                    GitHub 分支
                                         ↓ PR + 检查 + 合并
                                        main
```

| 命令 | 作用 | 不代表什么 |
|---|---|---|
| `git status` | 看当前分支、暂存与未提交改动 | clean 不代表 PR 已合并 |
| `git fetch origin` | 下载远端提交，更新远端跟踪引用 | 不自动更新当前分支的文件 |
| `git switch main` | 切换本地主分支及对应文件版本 | 不自动获取 GitHub 最新 main |
| `git pull --ff-only origin main` | 获取远端 main 并快进整合到当前分支 | 分叉时会停止，不自动生成合并提交 |
| `git switch -c ci/day11-actions` | 从当前提交建分支并切换过去 | 建分支本身不产生新提交 |
| `git add <文件>` | 把指定文件的当前变化放入暂存区 | 不上传文件 |
| `git commit` | 将暂存区变化保存为本地提交 | 不自动推送或创建 PR |
| `git push -u origin <分支>` | 推送并设置 upstream 跟踪关系 | 不自动合并进 main |
| `git push` | 通常按当前分支的 upstream 推送 | 仍需确认分支及 Git 配置 |
| `git rm <文件>` | 删除文件并暂存删除操作 | 不删除历史提交里的文件 |

- `origin` 是远端仓库的别名。
- `main` 是本地分支；`origin/main` 是本地保存的远端分支最近同步位置，不是实时查询。
- `working tree clean`：没有未提交改动。
- `behind ... by 4 commits`：本地分支落后远端跟踪引用 4 个提交。
- `Fast-forward`：分支指针直接前移，没有为这次 pull 额外创建合并提交。
- `files changed / insertions / deletions`：此次更新的差异摘要，不等于现在有未提交修改。
- Git 输出中文路径时可能显示转义序列，不代表文件损坏。

**面试或操作时记住：commit 成功、push 成功、CI 成功是三个不同结果。**

## 二、Actions：配置在哪里，谁执行

配置位于仓库内：

```text
ops-notes/.github/workflows/notes-ci.yml
```

`.github` 是本地隐藏目录，也是 GitHub 的约定目录名；不是终端自带的 GitHub 目录。`mkdir -p .github/workflows` 只创建目录，必须写入 YAML、提交并推送后，GitHub 才会识别。

| 概念 | 含义 |
|---|---|
| Workflow | 整条流水线 |
| Event | 触发事件，例如 push、pull_request |
| Job | 在 runner 上执行的一组步骤 |
| Step | job 内的具体操作 |
| Runner | 执行环境，本次为 GitHub 托管的 Ubuntu，不是 Mac / ECS |

一个 job 的 steps 默认依次执行；不同 jobs 默认可并行，依赖关系用 `needs` 表示。

实际配置关键部分（完整文件见 [notes-ci.yml](../.github/workflows/notes-ci.yml)）：

```yaml
name: Notes CI
on:
  push:
    branches: [main, ci/day11-actions]
  pull_request:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
jobs:
  check-markdown:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
```

- checkout 将仓库取到 runner；setup-python 准备 Python 环境。
- `contents: read` 遵循最小权限，本次不用写仓库或部署权限。
- PR 的 branches 过滤目标分支。本次 push 和 PR 均可触发，因此同一变更可能有两轮检查。
- 手动入口 `workflow_dispatch` 通常需 workflow 已存在于默认分支。
- `ubuntu-latest` 会随托管镜像迁移；本次收到 Ubuntu 26 迁移 notice 后改为 `ubuntu-24.04`。这固定系统主要版本，不冻结所有工具。
- 旧 checkout@v4 / setup-python@v5 收到 Node.js 20 弃用 warning 后，改用上述版本。Action 自身的 Node.js 与检查脚本的 Python 是两套环境。

## 三、检查范围与真实失败记录

脚本通过 `git ls-files -z -- '*.md'` 获取已跟踪 Markdown 文件，检查：

1. 根目录 README 是否已跟踪。
2. 文件是否可按 UTF-8 解码。
3. 内容是否为空或只有空白。
4. 是否含 NUL 字符。

**不检查排版、链接有效性、内容事实或部署安全。未跟踪文件也不在列表内。**

四轮真实运行：

| 运行 / 提交 | 结果 | 已知原因 |
|---|---|---|
| #1 / `910b17a` 添加流水线 | 成功 | 日志显示 `PASS: checked 13 Markdown files` |
| #2 / `13d2662` 添加空文件、修改版本 | 失败 | 用户发现版本号多输入字母；具体错误日志未采集 |
| #3 / `c59fbf1` 修正拼写 | 失败 | 检查脚本发现空 Markdown 文件 |
| #4 / `bf481dc` 删除空文件 | 成功 | Actions 列表显示绿色；该截图未展示最终文件数量 |

第 #3 轮的直接证据：

```text
ERROR: ci-empty-demo.md: empty Markdown file
Checked: ci-empty-demo.md
Error: Process completed with exit code 1.
```

`Checked` 仅表示已检查，不表示通过。具体原因是第一行，退出码 1 是失败结果。stdout / stderr 的缓冲可能导致错误先于其他输出显示，不能仅按界面行序推断执行先后。

修复操作：

```bash
git rm ci-empty-demo.md
git commit -m "fix(ci): remove the empty Markdown test file"
git push
```

CI 失败不会撤销已推送提交；通常用新修复提交触发新运行。旧红色记录保留作为历史证据。对旧运行点击 Re-run 会使用旧提交配置，不会自动采用本地修正。

## 四、CI 检查与合并门槛是两回事

> 流水线负责检查；分支保护 / ruleset 负责要求检查通过。

本次在 main 的规则中设置：

- Enforcement status：Active。
- Target：Include by pattern，精确填写 `main`，不匹配所有练习分支。
- Require a pull request before merging：开启。
- Required approvals：0，单人仓库不要求他人批准。
- Require status checks to pass：开启，添加 `check-markdown`。
- 未要求分支合并前与 main 保持最新，未添加绕过者；这些是本次学习选择，不是所有团队的通用规范。

PR #9 的截图直接显示：

```text
Notes CI / check-markdown (pull_request)   Successful   Required
Notes CI / check-markdown (push)           Successful   Required
All checks have passed
No conflicts with base branch
```

绿色勾只表示成功，`Required` 才表明被识别为必需检查。两条来自不同触发事件。

本次验证了必需检查标记，**没有在启用规则后故意再制造失败测试拦截**。不能把配置生效写成已经实测失败阻止合并。

## 五、日常使用与面试速记

- 改 workflow 或笔记后，提交并推送到当前 PR 分支，现有 PR 自动更新，不必重新创建。
- 合并前检查最新提交的状态；历史红叉不等于当前版本失败。
- 新增或改名 job 时同步检查 ruleset，避免要求一个不再运行的检查。
- 先读失败 step 的具体错误，再看退出码；区分环境准备失败与业务检查失败。
- CI 是持续集成检查；CD 才涉及交付 / 部署，今天未做自动部署。
- 后续可增加链接校验、测试、构建或部署，但每次应明确权限、检查范围与失败处理，不能把绿色 CI 当成绝对正确保证。
