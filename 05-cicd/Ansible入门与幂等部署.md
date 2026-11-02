# Ansible 入门：inventory、playbook 与幂等性

> Day 17，2026-11-02。控制节点为 Mac，被管理主机为一台 ECS（Ubuntu 22.04）。
> Mac 受企业环境限制不运行 Docker，Ansible 从 Mac 通过 SSH 管理 ECS，容器部署在 ECS 执行。
> 输出为用户实际终端结果，另有只读命令二次核实；未验证项明确标注。

## 一、它解决的问题

数百家机构的批量交付，手工 SSH 逐台执行有三个问题：

| 问题 | 后果 |
|---|---|
| 命令靠记忆重打 | 各机器执行结果不一致 |
| 中途失败不知停在哪 | 部分成功、部分失败，状态不明 |
| 重复执行不安全 | 第二次跑可能改坏已配置的东西 |

Ansible 的核心价值是**幂等**：同一份 playbook 执行一次和十次，结果相同。

```text
手工：if 已安装 then 跳过 else 安装    ← 判断要自己写
Ansible：state: present               ← 模块内部判断，你描述目标状态
```

**面试答法：** Ansible 是声明式配置管理工具，通过 SSH 免 agent 管理远程主机。价值不只在于省命令，而在于幂等性——重复执行不破坏既有状态，因此批量操作可以安全重试。配合 `--check` / `--diff` 可在真实变更前预览。

### 与其他工具的定位差异

| 工具 | 作用范围 | 视角 |
|---|---|---|
| Docker Compose | 单机多容器 | 单机 |
| Kubernetes | 集群内维持期望状态 | 编排层 |
| Ansible | 一台机器管一批服务器 | 运维操作 |
| GitHub Actions | 事件触发自动执行 | 流程自动化 |

Ansible 不替代它们，而是把"在 N 台机器上执行同一套操作"变成可重复执行的内容。

## 二、四个概念

```text
inventory（有哪些主机）
   ↓
playbook（在这些主机上做什么）
   ↓
module（用什么能力）
   ↓
task（一次具体操作）
```

### inventory

```ini
[web]
web01 ansible_host=<SERVER_IP> ansible_user=<USER> ansible_ssh_private_key_file=~/.ssh/<KEY>
```

`[web]` 是组名，playbook 用 `hosts: web` 选中。真实交付会按机构或角色分组。

**真实 inventory 含服务器地址，不得提交到公开仓库。** 仓库中只保留 `inventory.example.ini` 模板，实际 `inventory.ini` 已加入 `.gitignore` 并用 `git check-ignore -v` 验证生效。

### module：优先用专用模块

| 类别 | 模块 | 自带状态判断 |
|---|---|---|
| 文件/目录 | `file`、`copy`、`template` | 是 |
| 包与服务 | `apt`、`systemd` | 是 |
| 命令 | `command`、`shell` | **否** |

`command` / `shell` 应作为最后手段，使用时必须补 `creates`、`changed_when`、`failed_when`，否则每次运行都报告变更。

## 三、环境搭建

控制端隔离安装在 Mac 的虚拟环境，不污染系统 Python：

```bash
python3 -m venv ~/.venvs/ansible
~/.venvs/ansible/bin/pip install 'ansible-core==2.18.*'
```

实测 `ansible --version` 为 **core 2.18.19**。被管理主机识别到 `/usr/bin/python3.10`，无需在 ECS 安装 agent。

**一个环境相关的报错：** 在受限的自动化环境里运行时出现

```text
ERROR: Ansible requires blocking IO on stdin/stdout/stderr.
Non-blocking file handles detected: <stderr>
```

原因是调用方的标准错误被设为非阻塞，不是 Ansible 缺陷。把 stderr 重定向到文件即可正常运行；普通终端执行不会出现。

## 四、连通性与 facts

```bash
ansible web -m ansible.builtin.ping
```

```text
web01 | SUCCESS => {
    "ansible_facts": {"discovered_interpreter_python": "/usr/bin/python3.10"},
    "changed": false,
    "ping": "pong"
}
```

`ping` 不是 ICMP，而是"能通过 SSH 连上、能执行 Python、能返回结构化结果"。

```bash
ansible web -m ansible.builtin.setup -a "filter=ansible_distribution*"
```

返回 `Ubuntu`、`22.04`、`jammy`、`major_version 22` 等。这些 facts 可在 playbook 中直接用 `{{ ansible_distribution }}` 引用。**免 agent 的关键就在这里**：临时连上去采集，不常驻进程。

## 五、一个必踩的语法坑：`{{ }}` 冲突

```bash
ansible web -a "docker ps --format '{{.Names}}'" -m ansible.builtin.shell
```

```text
FAILED! => template error while templating string: unexpected '.'
String: docker ps --format '{{.Names}}'
```

**Ansible 执行前会用 Jinja2 渲染参数，把 Docker 的 `{{.Names}}` 当作变量去求值。** 两种解法：

```bash
# A. 用 raw 告诉 Ansible 不要解析
ansible web -m ansible.builtin.shell \
  -a "docker ps --format '{% raw %}{{.Names}}{% endraw %}'"

# B. 避开模板语法
ansible web -m ansible.builtin.command -a "docker ps --no-trunc"
```

playbook 里写 `{{.Image}}` 需要写成 `"{{ '{{.Image}}' }}"`
——外层交给 Ansible，内层作为字符串拼出来。`deploy-ci-web.yml` 中的写法正是为此。

## 六、部署 playbook 的结构

`deploy-ci-web.yml` 把 Day 15 的手工部署流程编码为可重复执行的内容：

```text
检查镜像是否本地存在 → 缺失才 pull
解析目标镜像 ID
检查容器是否存在、运行的镜像 ID 是否等于目标
不一致才删除并重建
等待健康检查通过（重试）
断言响应内容、运行 UID
```

**判断重建的逻辑是核心**：

```yaml
needs_recreate: >-
  {{ running_container.rc != 0
     or (running_container.stdout | trim) != expected_id }}
```

**比较的是镜像 ID**，不是"完整镜像地址"。若拿 `ghcr.io/...@sha256:...` 与容器中的镜像 ID 比较，两者格式不同，永远不相等，于是每次运行都重建，产生无意义的重启。

### 实测结果

用户连续执行两次，结果一致：

```text
web01 : ok=11  changed=0  unreachable=0  failed=0  skipped=3
```

`Show decision` 输出：

```json
{"exists": true,
 "expected_id": "sha256:0416a982...",
 "running_id":  "sha256:0416a982...",
 "needs_recreate": false}
```

被跳过的三个任务是：拉镜像、删容器、建容器。

**需要如实说明：** 这个容器在环境准备阶段已由我先创建，用户首次执行时已处于"存在且版本一致"状态，因此**没有采集到 `changed=1` 的首次创建**。上述两次运行证明的是"已达成目标状态时不产生变更"，完整对比（首次创建 → 二次无变更）本次未由用户观测到。

外部核实：

```text
ansible-ci-web   Up   127.0.0.1:18083->8080/tcp
/healthz → ok
```

与 Day 15 的手工部署 `day15-ci-web`（18080）并存，互不干扰。

## 七、幂等性的实测对比

### 三种写法

| 写法 | 第一次 | 第二次 | 幂等 |
|---|---|---|---|
| `command -a "touch a.txt"` | `CHANGED` | `CHANGED` | 否 |
| `file state=touch b.txt` | `CHANGED` | **`CHANGED`** | 否 |
| `command -a "... creates=c.txt"` | `CHANGED` | `skipped` | 是 |

第三种的实际输出：

```text
skipped, since /root/ansible-demo/c.txt exists
Did not run command since '/root/ansible-demo/c.txt' exists
```

### 一个被实测推翻的预期

我原先认为 `file: state=touch` 是幂等的（"文件存在就跳过"），**用户实测两次都是 `changed: true`**，随后补充验证确认了这一结果。

原因：`touch` 的语义就是更新时间戳，不论文件是否存在，每次都是新的修改时间，因此如实报告变更。

**结论：幂等不由"用了 Ansible"或"用了 file 模块"决定，而由这个操作能否判断目标状态已达成决定。** 时间戳每次不同，就永远算变更。

### 补充实测

我另行验证了两个能真正幂等创建文件的方式：

```text
file  state=file（文件已存在）      → changed: false
copy  content= force=no 第一次      → changed: true
copy  content= force=no 第二次      → changed: false
```

因此推荐：

| 场景 | 写法 |
|---|---|
| 建目录 | `file: state=directory` |
| 建文件、管内容 | `copy` / `template` |
| 管权限、属主、链接 | `file: state=file / link` |
| 必须执行命令 | `command` + `creates=` / `changed_when` |
| 只想刷新时间戳 | `file: state=touch`，接受其始终报变更 |

### 另一个实测到的失败

```text
Error, could not touch target: [Errno 2] No such file or directory: b'/root/ansible-demo/b.txt'
```

父目录不存在时，`state=touch` 直接失败，不会自动创建中间目录。**这暴露了自动化脚本常见假设错误：假定目标路径已就绪，实际不存在，导致批量执行中途失败。** 因此 playbook 应先显式建目录再写文件。

## 八、终端操作注意事项

把带 `#` 注释的整段代码粘进 zsh，会出现：

```text
zsh: command not found: #
```

交互式 zsh 默认不把 `#` 当注释，而会尝试当作命令执行。**逐条粘贴或去掉注释行**即可，不影响 Ansible 执行。

## 九、面试速记

1. **幂等**：执行一次与多次结果相同；判断依据是模块能否识别目标状态。
2. **优先专用模块**，`command`/`shell` 需补 `creates`/`changed_when`。
3. **比较镜像 ID 而非镜像地址**，否则会每轮重建。
4. **免 agent** 依赖目标机 Python，通过 SSH 临时执行。
5. **inventory 含真实地址，不进公开仓库**；仓库只保留模板。
6. `--check` 是预览，可靠性取决于模块对 check 模式的支持，**不能替代真实执行验证**。

## 十、本次未覆盖

- `template` 渲染配置、`handler` 触发服务重启。
- `block` / `rescue` / `always` 的失败处理与部分主机失败时的行为。
- `serial` 分批滚动、`max_fail_percentage` 控制中断。
- 多台真实主机的并发执行与性能表现。
- Vault 加密敏感变量。
- 从 GitHub Actions 触发 Ansible 部署（与现有 CI 链路衔接）。

以上留待后续练习；本次只验证了单机 inventory、ad-hoc 命令与幂等部署的基本机制。
