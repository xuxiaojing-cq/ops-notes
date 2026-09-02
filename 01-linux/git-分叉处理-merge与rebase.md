# Git 分叉（diverged）处理：merge 与 rebase 的选择

> 场景：本地有提交未推送，同时在 GitHub 网页端直接改动了文件，导致 push 被拒绝
> 时间：2026-09-02

## 一、现象

本地提交后执行 `git push`，被拒绝：

```
! [rejected]        main -> main (fetch first)
error: failed to push some refs to 'github.com:xxx/ops-notes.git'
hint: Updates were rejected because the remote contains work that
      you do not have locally.
```

## 二、先诊断，不要盲目操作

关键原则：**先用只读命令看清状态，再决定手段。**

`git fetch` 只更新远程分支的引用信息，不会修改本地工作区和本地分支，是安全的：

```bash
git fetch origin

# 本地有、远程没有的提交
git log --oneline origin/main..main

# 远程有、本地没有的提交
git log --oneline main..origin/main
```

诊断结果：

```
本地独有：
  ffcbeec docs(linux): 新增排查记录

远程独有：
  073d414 Add troubleshooting guide ...     ← 网页端新建文件
  10a45e3 Delete 01-linux/《...》            ← 网页端删除文件
```

即两条线从同一个起点各自前进，构成 **分叉（diverged）**：

```
                    ┌── ffcbeec  (本地 main)
749e358 ────────────┤
(共同祖先)          └── 073d414 ── 10a45e3  (origin/main)
```

Git 无法自行判断该如何整合，因此拒绝 push。

> 注意：此时**没有任何提交丢失**，两边的提交都完整存在，只是需要整合。

## 三、两种处理方式

### 方式一：merge（`git pull` 默认行为）

```bash
git pull            # 等价于 git fetch + git merge
```

将两条线合并，额外产生一个 merge commit：

```
749e358 ──┬── ffcbeec ──────┬── 9f3a1b2 (Merge branch 'main')
          └── 073d414 ── 10a45e3 ──┘
```

### 方式二：rebase

```bash
git pull --rebase   # 等价于 git fetch + git rebase
```

把本地独有的提交"摘下来"，重新应用到远程最新提交之后，历史变为线性：

```
749e358 ── 073d414 ── 10a45e3 ── b428ead (原 ffcbeec，提交 ID 已变)
```

⚠️ 注意提交 ID 从 `ffcbeec` 变成了 `b428ead`。rebase 是**重新生成提交**，
不是移动原提交，因此 ID 必然改变。这是理解 rebase 风险的关键。

## 四、对比与选择

| 维度 | merge | rebase |
|---|---|---|
| 历史形态 | 保留分叉，出现菱形结构 | 线性 |
| 额外提交 | 产生 merge commit | 无 |
| 提交 ID | 不变 | **改变** |
| 保留真实轨迹 | 是 | 否 |
| 适用场景 | 多人协作的公共分支 | 个人分支同步上游、保持历史整洁 |

### 本次选择 rebase 的理由

个人仓库，这个"分叉"没有协作意义（仅是网页端误操作产生），
不需要在历史里保留一次 merge 记录。

### 执行结果验证

```bash
git log --oneline --graph -6
# 输出全为竖线，无 |\ 分叉结构 → 线性历史

git fetch origin -q
git log --oneline origin/main..main | wc -l   # 0，本地不领先
git log --oneline main..origin/main | wc -l   # 0，远程不领先
git status                                     # up to date, clean
```

## 五、⚠️ 重要禁忌

### 1. 不要对已推送到远程的提交做 rebase

rebase 会改写提交 ID，等于改写历史。如果这些提交已被他人拉取，
会导致他人本地仓库与远程不一致，产生难以收拾的混乱。

**判断标准**：只对"仅存在于本地、尚未 push"的提交做 rebase。

### 2. 不要用 `git push -f` 解决 push 被拒

```bash
git push -f     # 危险：用本地强行覆盖远程
```

force push 会直接抹掉远程独有的提交。在团队协作的公共分支上执行，
可能造成他人工作丢失，属于典型的高危操作。

正确做法始终是：**先整合（pull / pull --rebase），再 push**。

## 六、根本原因与预防

本次问题的根源是**在 GitHub 网页端直接修改文件**，
导致远程产生了本地不存在的提交。

预防措施：

- 统一在本地修改 → 本地提交 → push，保持单向流动
- 如确实在网页端改过，下次动手前先 `git pull`
- 多设备协作时，开始工作前习惯性执行一次 `git pull`

## 七、可复用的排查套路

遇到任何 Git 同步异常，先执行这一组只读命令：

```bash
git status                                  # 本地工作区状态
git fetch origin                            # 更新远程引用（安全，不改本地）
git log --oneline origin/main..main         # 本地独有提交
git log --oneline main..origin/main         # 远程独有提交
git log --oneline --graph --all -10         # 可视化整体历史
```

看清两边差异后再决定用 merge 还是 rebase。

> 这与线上故障排查的思路一致：**先定性、看清状态，再决定处置手段**，
> 而不是先动手再看结果。
