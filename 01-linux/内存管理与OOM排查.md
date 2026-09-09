# Linux 内存管理与 OOM 排查

> 实践环境：Ubuntu 22.04.5 / 2 vCPU / 1.6G 内存 / **Swap = 0** / 40G 云盘
> 日期：2026-09-09

## 一、free 各列的真实含义

```
        total   used   free   shared  buff/cache  available
Mem:    1.6Gi   424Mi  76Mi   2.0Mi   1.1Gi       991Mi
```

| 列 | 含义 |
|---|---|
| total | 物理内存总量 |
| used | 进程实际占用 = total - free - buff/cache |
| free | 完全未被使用过的内存 |
| shared | tmpfs 占用（/dev/shm 等） |
| buff/cache | 内核用于文件缓存的内存（**可回收**） |
| **available** | ⭐ 真正可用 ≈ free + 可回收的 cache |

### ⭐ 核心认知：free 少不代表内存不足

Linux 的设计原则是「空闲内存就是浪费的内存」。
只要有富余，内核就会用来做 page cache 加速磁盘访问，
这部分在进程需要内存时会被立即回收。

因此：
- **free 小** → 内存被充分利用，正常现象
- **available 小** → 才是真正的内存紧张

⚠️ 常见误操作：看到 free 只剩几十 MB 就执行
`echo 3 > /proc/sys/vm/drop_caches` 清缓存。
这不仅没有必要，还会导致后续磁盘 IO 明显变慢——
缓存被清空后所有读取都要重新访问磁盘。

## 二、实测：page cache 对读性能的影响

### 实验设计

⚠️ 第一次实验设计有误：先读文件再清缓存，导致第一次读取时
缓存状态不确定，两次测量不具备可比性（实测 2.2s vs 3.6s，无规律）。

修正方案：**先清空缓存，确保第一次为冷读**，且文件大小
控制在可用内存以内（200MB < 991MB available），保证能完整缓存。

```bash
dd if=/dev/zero of=/tmp/testfile bs=1M count=200
sync && echo 3 > /proc/sys/vm/drop_caches   # 确保初始状态一致

time cat /tmp/testfile > /dev/null    # 冷读
time cat /tmp/testfile > /dev/null    # 热读
```

### 结果

| | real | user | sys | real - sys |
|---|---|---|---|---|
| 冷读（磁盘） | 0.860s | 0.000s | 0.486s | **0.374s** |
| 热读（缓存） | 0.233s | 0.001s | 0.231s | **0.002s** |

**总耗时相差 3.7 倍。**

### ⭐ 更有价值的观察：real 与 sys 的差值

`time` 的三个值：
- `real` — 墙钟时间（实际经过的时间）
- `user` — 用户态 CPU 时间
- `sys` — 内核态 CPU 时间
- **`real - (user + sys)` — 等待时间**（IO 等待、锁、调度延迟）

冷读时 `real` 比 `sys` 多出 0.374 秒，这部分就是等待磁盘返回的时间。
热读时 `real ≈ sys`，等待时间几乎为零，
说明数据直接来自内存，CPU 时间全部消耗在内核态的数据拷贝上。

> 这个差值比总耗时更能说明问题：它直接区分了
> "CPU 在干活" 和 "CPU 在等待"。

### 结论

page cache 对读密集型业务的性能影响显著。
生产环境中不应随意清理缓存，
`free` 偏低而 `available` 充足是健康状态。

## 三、OOM Killer 机制

### 触发条件

物理内存与 swap 全部耗尽，且内核无法回收出更多可用内存时，
为避免整机崩溃，内核主动选择并终止某个进程。

⚠️ 云服务器通常默认不配置 swap（本机 Swap = 0），
意味着物理内存耗尽即刻触发 OOM，**没有缓冲区**。
这也是云上 OOM 往往表现得很突然的原因。

### 牺牲进程的选择

内核为每个进程计算 `oom_score`（0-1000），分数最高者被终止。

- 占用内存越多，分数越高（主要因素）
- `oom_score_adj` 可人为干预（-1000 ~ 1000）
  - `-1000`：完全免疫
  - `+1000`：优先被杀

```bash
cat /proc/PID/oom_score
cat /proc/PID/oom_score_adj

# 列出当前最容易被杀的进程
for p in /proc/[0-9]*; do
  pid=${p#/proc/}
  score=$(cat $p/oom_score 2>/dev/null)
  name=$(cat $p/comm 2>/dev/null)
  [ -n "$score" ] && echo "$score $pid $name"
done | sort -rn | head -10
```

⭐ 实践建议：为 sshd、监控 agent 等关键进程设置保护，
保证机器发生 OOM 时仍能登录处置。

```bash
for pid in $(pgrep sshd); do echo -1000 > /proc/$pid/oom_score_adj; done
```

### 确认是否发生过 OOM

```bash
dmesg -T | grep -i -E "out of memory|killed process"
grep -i "killed process" /var/log/syslog | tail -20    # Debian/Ubuntu
grep -i "killed process" /var/log/messages | tail -20  # RHEL 系
journalctl -k | grep -i "killed process"
```

典型日志：

```
Out of memory: Killed process 12345 (java) total-vm:2097152kB,
anon-rss:1048576kB, file-rss:0kB, shmem-rss:0kB, oom_score_adj:0
```

| 字段 | 含义 |
|---|---|
| total-vm | 虚拟内存总量（申请量，不代表实际占用） |
| **anon-rss** | ⭐ 匿名页物理占用，**真正消耗的内存** |
| file-rss | 文件映射占用 |
| oom_score_adj | 当时的调整值 |

⚠️ 判断内存占用应看 `anon-rss` 而非 `total-vm`。
JVM 等程序常申请大片虚拟地址空间但并未实际使用，`total-vm` 会严重虚高。

## 四、⭐ 系统 OOM 与 JVM OOM 的区别

| | JVM 堆 OOM | 系统 OOM |
|---|---|---|
| 触发条件 | 堆使用达到 `-Xmx` 上限 | 整机物理内存耗尽 |
| 执行者 | JVM 自身抛出异常 | 内核强制终止 |
| 进程状态 | 通常仍存活（可能假死） | **直接消失** |
| 应用日志 | `java.lang.OutOfMemoryError` | ⚠️ **可能无任何记录** |
| 内核日志 | 无 | `killed process` |
| 退出码 | 视处理方式 | **137**（128+9，SIGKILL） |
| 能否 HeapDump | 可以 | ⚠️ **来不及，进程瞬间被杀** |

### 三步判定法

```
第 1 步  查内核日志 dmesg -T | grep -i "killed process"
         有记录 → 系统级 OOM，进入第 2 步
         无记录 → 检查应用日志的 OutOfMemoryError → JVM 内部 OOM

第 2 步  确认是谁耗尽了内存
         被杀进程即主因 → 排查其内存增长原因
         被杀的是无关进程（如 sshd）→ 真凶是其他进程，需查同期内存曲线

第 3 步  Java 进程被系统 OOM 时，区分堆内 / 堆外
         RSS ≈ Xmx + 元空间 + 线程栈  → 堆内问题
         RSS 明显超出 Xmx    → ⭐ 堆外内存问题，调 -Xmx 无效
```

### ⭐ systemctl status 中的关键信息

```
Main PID: 12345 (code=killed, signal=KILL)
```

| 显示 | 含义 |
|---|---|
| `code=killed, signal=KILL` | 被强制终止（OOM Killer 或 kill -9） |
| `code=killed, signal=TERM` | 收到终止信号，正常停止 |
| `code=exited, status=1` | 程序自行退出，应用层错误 |
| `code=exited, status=137` | 137 = 128+9，同样是 SIGKILL |

## 五、⭐ OutOfMemoryError 的类型区分

`OutOfMemoryError` 后的描述决定了排查方向，不可一概而论。

| 报错 | 含义 | 处置方向 |
|---|---|---|
| `Java heap space` | 堆空间不足 | 调 `-Xmx` 或排查泄漏 |
| **`GC overhead limit exceeded`** | ⭐ GC 占用 >98% 时间却回收 <2% 空间 | **内存泄漏强信号** |
| `Metaspace` | 元空间耗尽（类加载过多） | `-XX:MaxMetaspaceSize` |
| `Direct buffer memory` | 堆外直接内存不足 | `-XX:MaxDirectMemorySize`，查 NIO 泄漏 |
| `unable to create new native thread` | 线程数达上限 | 查线程泄漏、`ulimit -u` |
| `Requested array size exceeds VM limit` | 申请超大数组 | 代码问题 |

### ⭐ GC overhead limit exceeded 的判读价值

这个报错比 `Java heap space` 信息量更大：

- 若仅是堆偏小，GC 能正常回收出空间，
  程序表现为频繁 GC 导致变慢，但不会立即崩溃
- 若 GC 消耗了绝大部分时间却回收不出空间，
  说明**堆中大量对象被强引用持有、无法回收**

因此 `GC overhead limit exceeded` 指向的是
**内存泄漏或对象无限堆积**，而非单纯的参数配置偏小。
此时调大 `-Xmx` 只能推迟问题发生，无法根治。

### 堆内 vs 堆外的判定

关键是对比进程实际物理内存占用（RSS）与 `-Xmx`：

- **RSS ≈ Xmx + 元空间 + 线程栈等开销** → 堆内问题
- **RSS 显著超出 Xmx**（如 Xmx=1G 但 RSS=3G）→ 堆外问题

堆外内存的常见来源：NIO 的 DirectByteBuffer、JNI 调用、
线程数过多导致的栈内存累积。

⚠️ 堆外问题调整 `-Xmx` 不仅无效，反而可能加重——
堆占得越大，留给堆外的空间越少。

## 六、JVM 排查常用命令

```bash
jps -v                    # 列出 Java 进程及启动参数
jcmd PID VM.flags         # 查看 JVM 参数
jstat -gc PID 1000 5      # 堆各区使用情况与 GC 次数（每秒一次，共5次）
jmap -heap PID            # 堆配置与当前使用
jmap -histo:live PID | head -20   # 按类统计对象数量（会触发 Full GC，慎用）
```

### 生产环境必配参数

```
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=/data/dump/
```

⚠️ **重要限制**：该配置仅在 JVM 自身抛出 OOM 时生效。
若进程被系统 OOM Killer 以 SIGKILL 终止，**来不及生成 dump 文件**。

> ⭐ 这直接决定了排查策略：系统 OOM 事后没有现场可查，
> 只能依靠事前的持续观测数据。

## 七、systemd 层面的防护配置

### 自动重启

服务被杀后若停留在 failed 状态，说明未配置重启策略，
故障恢复完全依赖人工发现。

```ini
[Service]
Restart=on-failure          # 异常退出时自动重启
RestartSec=10s              # 重启间隔
StartLimitBurst=5           # 限制：300秒内最多重启5次
StartLimitIntervalSec=300
```

⚠️ 后两个参数不可省略。若不限制重启次数，
服务反复崩溃时会不断重启，反而加重系统负担、掩盖问题。

### 内存限制与 OOM 保护

```ini
[Service]
OOMScoreAdjust=-500         # 降低被 OOM Killer 选中的概率
MemoryMax=2G                # cgroup 层面限制内存上限
```

⭐ `MemoryMax` 的价值在于**故障隔离**：
单个服务超出限制时只会触发自身的 cgroup OOM，
不会耗尽整机内存而波及其他服务。
这是容器化之前就已存在的资源隔离手段。

查看配置：

```bash
systemctl cat 服务名
systemctl show 服务名 | grep -E "Restart|OOM|Memory"
```

## 八、方法论小结

### 观测能力决定可排查性

偶发性 OOM 问题的核心困难在于**事后无现场**：

- 系统 OOM 使用 SIGKILL，进程瞬间消失，无法生成 dump
- 重启恢复后，所有内存状态归零，线索全部丢失

因此这类问题必须依赖**事前建立的持续观测**：

| 指标 | 作用 |
|---|---|
| 进程 RSS 曲线 | 判断内存增长形态 |
| JVM 堆使用率 | 区分堆内/堆外 |
| GC 次数与耗时 | 识别回收效率恶化 |
| 系统 available 内存 | 判断整机压力 |

### ⭐ 通过曲线形态判断原因

| 形态 | 指向 |
|---|---|
| 缓慢线性爬升 | 内存泄漏 |
| 锯齿状但基线逐渐抬高 | 部分对象无法回收，泄漏 |
| 突然垂直上涨 | 单次大量分配（大请求、大批量数据） |
| 锯齿状且基线平稳 | 正常的分配-回收循环 |

### 处置顺序

面对偶发且不可复现的问题，正确顺序是：

1. **先补观测**，而非先改配置
2. 配置 HeapDump 保留堆内 OOM 现场
3. 增加自愈能力（Restart 策略），缩短故障时长
4. 等待下次发生，用数据定位方向
5. 有方向后再动手处置

⚠️ 直接调整参数的问题在于**无法验证有效性**——
问题本身偶发，改完之后长期不复现，
无法区分是真的修复了，还是恰好没有触发。
