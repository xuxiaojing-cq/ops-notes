# GitHub Actions 自动测试与检查报告

> Day 12，2026-10-22。沿用 ops-notes 仓库，在 `ci/day12-tests-artifacts` 分支练习。
> 本次完成的是自动测试和报告产物，不是应用编译、镜像构建或自动部署。
> 实验数字与失败信息来自实际终端、Actions 截图和下载报告；未验证项不写成已完成。

## 一、从内嵌代码到可测试脚本

```text
.github/workflows/notes-ci.yml → 安排环境与执行顺序
scripts/check_notes.py       → 检查文件、打印结果、返回退出码
tests/test_check_notes.py    → 验证检查函数的行为
reports/notes-check.txt      → 运行时生成的报告，不提交到 Git
```

`check_file(path)` 返回问题列表，空列表表示通过；`main()` 获取已跟踪 Markdown 文件、输出日志并返回 0 或 1。

```python
if __name__ == "__main__":
    sys.exit(main())
```

直接运行脚本时执行 main，被测试代码导入时不自动检查整个仓库。

本地从仓库根目录执行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check_notes.py
```

`discover -s tests` 查找测试，`-v` 展示各测试结果。这里测试的是函数行为，仓库检查则是检查当前提交的数据，目的不同。

## 二、五个测试与一次回归实验

| 用例 | 输入 | 预期 |
|---|---|---|
| valid_markdown | 正常中文 Markdown | `[]` |
| empty_file | 空字节 | 空文件错误 |
| whitespace_only | 空格、换行、制表符 | 空文件错误 |
| invalid_utf8 | `b"\xff"` | 编码错误 |
| nul_character | 含 NUL 字符 | 异常字符错误 |

测试使用 `TemporaryDirectory()` 写入临时文件，结束自动清理，不污染仓库。`assertEqual` 比较实际结果和预期结果，不一致就使测试失败。

### 实测：改坏空白判断

将正确逻辑：

```python
if not text.strip():
```

临时改为：

```python
if not text:
```

前者将纯空白内容判为空，后者只判断字符串是否完全为空。

实际失败：

```text
test_whitespace_only ... FAIL
AssertionError: Lists differ: [] != ['empty Markdown file']
Ran 5 tests in 0.014s
FAILED (failures=1)
```

恢复后：

```text
Ran 5 tests in 0.011s
OK
```

这证明测试能发现逻辑回归，而不只是“代码能运行”。未采集错误版本检查真实仓库的输出，不把“仍通过仓库检查”写成实测。

### 验证边界

五个用例覆盖了 `check_file()` 的指定行为，不是完整测试覆盖率。当前没有测试 main 的 README 检查、Git 调用失败或文件读取失败等情况。

脚本只检查 `git ls-files` 返回的已跟踪 Markdown；不验证排版、链接或事实真实性。它也没有捕获所有 I/O 错误，后续可单独完善，不在本次实验中假装已具备。

## 三、流水线执行顺序与失败语义

现有完整配置见 [notes-ci.yml](../.github/workflows/notes-ci.yml)。保留 job 名 `check-markdown`，避免已有必需检查规则等待旧名称。

```text
Checkout → Python 3.12 → 单元测试 → 检查并生成报告 → 上传 Artifact
```

关键步骤：

```yaml
- name: Run unit tests
  run: python -m unittest discover -s tests -v

- name: Check Markdown and generate report
  shell: bash
  run: |
    mkdir -p reports
    set -o pipefail
    python scripts/check_notes.py 2>&1 | tee reports/notes-check.txt

- name: Upload check report
  if: ${{ !cancelled() }}
  uses: actions/upload-artifact@v4
  with:
    name: notes-check-report
    path: reports/notes-check.txt
    if-no-files-found: warn
    retention-days: 7
```

### 管道：不能让 tee 掩盖失败

- `2>&1` 合并错误输出与标准输出，让报告也包含错误信息。
- `tee` 同时显示日志和保存文件。
- `pipefail` 让管道中 Python 的失败传递给步骤；否则通常只看最后一个命令的退出码，可能出现 Python 失败、tee 成功、步骤误绿。
- stdout / stderr 缓冲不同，日志显示顺序不一定等同于代码执行先后。

### 上传失败报告

普通步骤默认受成功条件约束；检查失败后，上传步骤使用 `!cancelled()` 仍尝试执行，但取消运行时不强行执行。

若单元测试已失败，后面的普通报告步骤通常跳过，没有报告文件时配置为 warn。**该分支仅解释配置逻辑，本次没有在 CI 中实际制造单元测试失败验证它。**

上传成功不会清除前面检查步骤的失败。

## 四、Artifact 与日志、Git、缓存的区别

| 类型 | 用途 |
|---|---|
| Actions 日志 | 查看执行过程与具体错误 |
| Artifact | 保存、下载生成文件，例如报告或构建产物 |
| Git 文件 | 版本管理源码、配置和文档 |
| Cache | 复用依赖等以加速后续任务，不是本次报告下载入口 |

Runner 工作目录是临时执行环境，不能假定下一次运行保留文件。本次报告通过 Summary → Artifacts → `notes-check-report` 下载，内容为 `notes-check.txt`。

保留时间配置为 7 天，实际还受平台策略等约束，不是永久存储。报告上传前应检查是否包含凭据或敏感数据，不能因其不是 Git 文件就认为没有泄露风险。

## 五、CI 的成功与失败对照

### 首轮成功：提交 e297c05

实际 CI 测试：

```text
Ran 5 tests in 0.002s
OK
```

检查日志和下载报告都包含：

```text
PASS: checked 14 Markdown files
```

报告包含各笔记与 README 的检查明细，用户实际下载并核对。证明测试执行、文件检查和产物保存链路可用。

### 失败报告实验：提交 897ab25

新增并提交空文件 `ci-artifact-empty-demo.md`。实际观察：

| 步骤 | 结果 |
|---|---|
| Run unit tests | 成功，代码没有改坏 |
| Check Markdown and generate report | 失败，退出码 1 |
| Upload check report | 成功；用户下载报告并提供错误行 |

```text
ERROR: ci-artifact-empty-demo.md: empty Markdown file
Error: Process completed with exit code 1.
```

这是“测试代码正确，但真实数据不合格”的场景。管道保留失败状态，上传步骤继续保留排障证据。

### 修复：提交 2aeec35

```bash
git rm ci-artifact-empty-demo.md
# 提交删除操作并 push
```

修复截图中测试、检查和上传均成功，检查显示 14 个文件通过。截图顶部仍为运行中状态，**未凭该截图断言整轮最终状态已成功**。

上传期间出现 Node.js `punycode` 弃用 warning，但没有阻止本次上传，不是空文件错误。

## 六、Git 与终端操作补充

### 忽略生成目录

原有 `__pycache__/` 已匹配两个缓存目录，新增仓库根目录报告规则：

```gitignore
/reports/
```

```bash
git check-ignore -v scripts/__pycache__/probe.pyc \
  tests/__pycache__/probe.pyc reports/notes-check.txt
```

显示规则文件、行号、匹配规则和路径。测试路径不需要真的存在，也不会创建文件。两行输出可以匹配同一条规则，不等于有两条不同规则。

`git status` 展示改动，不展示忽略规则；`git diff --cached --stat` 展示暂存范围和数量，不验证代码逻辑。提交前仍要运行测试与 `git diff --cached --check`。

### Mac zsh 清空文件

本次裸 `> 文件` 在用户环境中等待输入，符合 zsh 对无命令重定向使用默认命令的行为。行为取决于 Shell 与配置，不是 macOS 不支持重定向。

明确清空写法：

```bash
: > .github/workflows/notes-ci.yml
```

它立即截断文件，没有确认提示，必须核对路径，并填入新配置后再提交。`>>` 是追加，`rm` 是删除文件本身。

## 七、面试速记与下一步

1. 自动测试用已知输入验证代码，真实仓库检查验证当前数据，两者不能互相替代。
2. 退出码是执行结果，具体错误日志才帮助定位根因。
3. 使用管道时要防止末尾成功命令掩盖前面的失败。
4. 失败时也应保留必要报告；Artifact 成功不代表 CI 成功。
5. Git 忽略生成物，Artifact 保存交付或排障文件，Cache 加速任务，三者用途不同。
6. 本次仅完成测试与报告产物；应用构建、部署与回滚仍待后续练习。
