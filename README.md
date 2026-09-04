# MemoryScope

MemoryScope 是一个面向小内存 VPS 的 AstrBot 内存观察插件。它不承诺一个不存在的“每个插件独占 RSS”精确数字，而是把问题拆成几种可验证、开销各不相同的口径：

- **进程真实足迹**：`/proc/self/smaps_rollup` 的 `Pss + SwapPss`，默认常驻采样；
- **进程 RSS / cgroup**：内核记账的驻留页、匿名/文件页、swap 与容器额度；
- **引用图保留量**：从插件实例和模块出发，估算仍被它保持可达的对象，默认后台轮转，带时间切片与占空比；
- **加载期导入成本**：只在启动加载窗口短暂包裹 `builtins.__import__`，用 RSS 前后差找出大插件和大依赖；
- **对象归属普查**：手动触发后，按对象类型所属模块估算存活 Python 对象；
- **顶层依赖审计**：只读 AST 源码，找出可以改成惰性导入的重依赖。

页面入口是 Dashboard 插件页中的 **MemoryScope**，聊天入口是管理员命令组 `/mem`。

## 先说结论：不能精确算“插件独占 RSS”

RSS 是进程级的页集合，不带“这 4 KB 属于哪个 Python 插件”的标签。多个插件共享同一份解释器、第三方库、内存分配器 arena、线程栈和 C 扩展缓冲区时，任何插件都无法仅靠一个轻量 Python 插件把它们准确拆开。

所以页面把每个数字的口径写在旁边：

| 口径 | 能回答什么 | 不能回答什么 | 默认行为 |
| --- | --- | --- | --- |
| 真实足迹 Pss+SwapPss | 进程实际占用了多少物理内存 + swap | 哪个插件占了这些页 | 读 `smaps_rollup`，约 5 ms，30 秒缓存 |
| 进程 RSS | 此刻驻留在物理内存里的量 | 已被换出的部分（完全不计入） | 每个采样点读一次 `/proc/self/statm` |
| 引用图保留量 | 从插件根对象可达的独占/共享字节下界 | native 内存与全部间接拥有关系 | 每 5 个采样点一轮，15 ms 切片 / 25% 占空比 |
| 加载期导入成本 | 某插件首次加载让 RSS 涨了多少 | 它现在还保留多少；共享包不能重复分摊 | 启动期短暂启用，加载完成即卸载 |
| 对象普查 | 哪些插件模块拥有较多 GC 跟踪对象 | 未跟踪对象、字符串/bytes、C 缓冲区 | 关闭，按钮或 `/mem census` 手动执行 |
| 归因覆盖率 | 逐插件数字一共覆盖了多少私有脏页 | 剩下那部分具体属于谁 | 每轮扫描后自动计算并公开显示 |

判断小鸡会不会被打爆，先看**足迹、已换出和足迹增长趋势**；不要把对象普查或导入账本的数字加起来当作进程总内存。

## 为什么彻底移除了 tracemalloc

早期 v1.0.0 在插件构造函数里直接执行 `tracemalloc.start(12)`。这会让此后的每一次 Python 内存分配都记录最多 12 帧调用栈；插件导入阶段正是分配最密集的阶段，在小内存机器上会迅速进入换页雪崩。

真实事故数据（2 核 / 约 1.6 GB 内存、123 个插件）：

| 分组 | 有追踪本次实测 | 无追踪基线 | 倍数 |
| --- | ---: | ---: | ---: |
| 追踪开启前的 17 个插件 | 4 s | 4.7 s | 0.85× |
| 追踪开启后的 104 个插件 | 1259 s | 34.5 s | **约 36×** |
| 整体插件加载 | 1265 s（约 21 分钟） | 39 s | **约 32×** |

期间 Dashboard 端口约 25 分钟没有 listen，swap 从约 2.0 GB 增至 3.3 GB。这个代价与“插件独占内存并不精确”的收益完全不匹配，所以之后的版本 **不导入、不调用、也不在页面提供开启入口**，后台同样不做常驻全堆扫描。

## v3 的三处精度升级

### 1. 主口径从 RSS 换成 Pss + SwapPss

RSS 不包含已经被内核换出的页。同一台 2 核 / 1676 MB 的机器上实测（`/proc/598/smaps_rollup`）：

| 字段 | 值 |
| --- | ---: |
| Rss | 601 MB |
| Pss | 598 MB |
| Private_Dirty | 476 MB |
| Swap / SwapPss | 375 MB |
| **足迹 = Pss + SwapPss** | **973 MB** |

只报 RSS 会把 372 MB 藏起来 —— 而在小机器上，恰恰是被换出的那部分决定了响应会不会变卡。`smaps_rollup` 是内核直接汇总好的十几行文本，读一次约 5.4 ms（对比 `/proc/self/status` 的 0.014 ms），所以它只在采样线程里读，带 30 秒缓存，页面上会显示这份数据的实际年龄。内核不提供该文件时（旧内核、非 Linux）自动退回 RSS，并且**绝不把 RSS 冒充成足迹**：没有 Pss 就不显示足迹。

### 2. 引用图保留量从“手动”改成“后台轮转”

v2 的逐插件归因只在你点按钮时才有，趋势图上的插件曲线永远是空的。v3 让它默认在后台跑，同时用三层限速保证它不会拖慢 bot：

- **每 5 个采样点才跑一轮**（60 秒间隔 ≈ 每 5 分钟）。插件的保留量以分钟为尺度变化，扫得更勤只是白烧 CPU；重启后第 2 个采样点会先跑一次，避免页面空 5 分钟。
- **公平配额 + 轮转起点**。对象预算按插件平分，每轮换一个起始插件，一个巨型插件不会饿死其余 126 个；本轮扫不完的插件下一轮补上，页面会标注“本轮有插件未扫完”。
- **15 ms 切片 + 25% 占空比**。遍历持有 GIL，一次 3 秒的整段扫描会让所有 handler 卡 3 秒；切成“工作 15 ms、让出 45 ms”之后，单次停顿只比 CPython 自己的线程切换间隔（5 ms）长一点点，代价是整轮墙钟时间变成 4 倍。

`gc.get_referents` 约 7.4 µs/对象，不修改分配路径，空闲时零成本 —— 这正是它和 tracemalloc 的本质区别：tracemalloc 给**每一次分配**都装钩子，引用图扫描只在它真的在扫的那 15 ms 里花钱。

### 3. 覆盖率是公开的，不是藏起来的

逐插件保留量之和永远小于进程私有脏页。页面直接显示 `覆盖私有脏页 = measured_bytes / Private_Dirty`，它**永远到不了 100%**：解释器自身结构、C 扩展在 Python 对象图之外分配的 arena、线程栈，从 `gc.get_referents` 根本不可达。看到 12% 不是 bug，而是这个方法诚实的上限。

## 常驻开销

| 操作 | 实测开销 | 是否常驻 |
| --- | ---: | --- |
| `/proc/self/statm`（RSS） | 约数微秒 | 是，每个采样点一次 |
| `/proc/self/status` | 0.014 ms | 是，每个采样点一次 |
| `smaps_rollup`（Pss/SwapPss） | 5.4 ms | 是，采样线程内，30 秒缓存 |
| `gc.get_allocated_blocks()` | 约 1 微秒 | 是 |
| 引用图扫描 | 7.4 µs/对象，15 ms 切片 + 25% 占空比 | 每 5 个采样点一轮 |
| 导入包装器 | 每次首导入约 2 微秒，启动总量几十毫秒 | 仅插件加载窗口 |
| `gc.get_objects()` 普查 | 60 万对象约 0.49 s（1/10 抽样约 19 ms） | 否，默认关闭 |
| 手动 GC | 几十到几百毫秒；RSS 不一定下降 | 否 |
| tracemalloc | —— | **从不使用** |

普通页面刷新只读缓存，不触发任何堆遍历。

## 已知环境实测

以下取自同一台小鸡（Debian 6.1.133 / 2 核 / 1676 MB / Python 3.11.2 / 127 个插件目录），用于解释数字的量级，不是所有机器的保证值：

| 指标 | 观测值 |
| --- | ---: |
| Rss | 601 MB |
| Pss | 598 MB |
| Pss_Dirty | 476 MB |
| Private_Clean / Private_Dirty | 118 MB / 476 MB |
| Shared_Clean | 4 MB |
| AnonHugePages | 38 MB |
| Swap / SwapPss | 375 MB |
| **足迹（Pss + SwapPss）** | **973 MB** |
| 新鲜进程首次 `import astrbot.api` 的 RSS 增量 | 约 155 MB（Windows 约 198 MB） |

### `import astrbot.api` 的 155/198 MB 是怎么回事

它**不是 MemoryScope 或任何插件凭空制造的**。那是在一个干净的 Python 进程里第一次 `import astrbot.api` 所付出的 RSS 增量：AstrBot API 模块会顺带拉入框架自身的模块树、依赖（pydantic、sqlalchemy、aiohttp 等）、类定义、缓存，以及 CPython 分配器为此保留的 arena。在已经跑着的 AstrBot 进程里再次 import 通常只是命中 `sys.modules`，不会第二次付这笔钱。

要判断插件的责任，应该看它自己的首次加载成本，以及它有没有把重依赖写在模块顶层，而不是把整个 `astrbot.api` 的冷导入成本算到某个插件头上。

导入账本中观察到的主要第三方包群：google 约 36.2 MB、sqlalchemy 约 19.2 MB、mcp 约 18.5 MB、anthropic 约 13.9 MB、openai 约 9.9 MB、aiohttp 约 8.4 MB、pypdf 约 6.2 MB、sqlmodel 约 6.2 MB。插件依赖的边际成本约 240.5 MB / 81 个包；pypinyin、faiss-cpu、sympy、pymupdf 是更值得优先审计的大户。导入账本合计约 395 MB，而实际足迹接近 1 GB，差额来自插件实例、缓存、连接、线程栈、解释器本体和 native 分配 —— 正是不能简单按插件平分的那部分。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/Whereis-Alice/astrbot_plugin_memory_scope
```

然后在 AstrBot 插件管理页启用/重载。依赖只有 AstrBot 通常已经自带的 `psutil`；缺失时插件会降级，仍会提供能读到的轻量数据。

## Dashboard 页面

页面为原生 HTML/CSS/JS，通过 Dashboard bridge 转发请求，插件**不额外开放任何未认证端点**。支持深色/浅色/跟随 Dashboard 三种皮肤，以及 10/30/60/300 秒自动刷新。

- **总览**：hero 区一个大数字（当前足迹）+ 数据来源标注（Pss+SwapPss / Pss / RSS 退化）；下面五张指标卡是常驻 RSS、已换出、归因保留、导入账本、归因覆盖率；再往下是多序列平滑趋势图（足迹 / RSS / 换出 / 分配块 / 归因保留），可悬停读值，附窗口均值、峰值、增量与斜率；还有基线设置/清除、手动 GC、Top 插件条形图和归因堆叠条。
- **插件**：可排序表格（导入成本 / 对象普查 / 对象数 / 引用图保留 / 增长趋势 / 近期迷你曲线），点行打开抽屉看单插件详情；未知值显示为 `-`，不会伪装成 0。
- **导入成本**：按第三方包和按插件两张表，显示启动期 RSS 差分、耗时、模块数、首个导入者。
- **依赖审计**：模块顶层重依赖清单与惰性化收益估算。
- **对象普查**：手动运行一次，看插件对象与其他来源的分布。
- **告警**：足迹/RSS 阈值、增长率、单插件体量与增长告警的当前状态。
- **口径说明**：逐项解释每个指标测了什么、测不到什么，以及小内存机器上的注意事项。

界面文案走 `.astrbot-plugin/i18n/` 下的 zh-CN / en-US 两份词典，代码内 fallback 与 zh-CN 值由测试锁定一致。

## 命令

命令组 `/mem`（别名 `/memoryscope`），全部需要管理员权限：

| 命令 | 作用 |
| --- | --- |
| `/mem top [N]` | 显示进程足迹/RSS 与重点插件（默认 `command_top_n` 条） |
| `/mem imports [N]` | 查看加载期包成本和插件加载汇总 |
| `/mem audit [N]` | 扫描模块顶层重依赖 |
| `/mem census [N]` | 手动执行一次对象普查；可能短暂停顿 |
| `/mem deep [N]` | 立刻补跑一轮引用图扫描；结果是下界 |
| `/mem plugin <名称>` | 查看单个插件的导入、对象、引用图和依赖详情 |
| `/mem gc` | 手动触发 GC 并比较 RSS 前后值 |
| `/mem base set\|clear` | 设置或清除对比基线 |

## 配置项

在插件管理页修改，配置会在插件下一次读取时生效：

| 键 | 默认 | 说明 |
| --- | ---: | --- |
| `proc_smaps_enabled` | `true` | 是否读 `smaps_rollup` 拿 Pss/SwapPss；关掉就只剩 RSS，被换出的部分不可见 |
| `proc_smaps_min_interval_seconds` | `30` | 两次 `smaps_rollup` 读取之间的缓存时长，避免页面刷新反复付那 5 ms |
| `sample_interval_seconds` | `60` | 采样间隔，范围 10～3600 |
| `history_size` | `720` | 内存中保留的采样点数（60 s × 720 ≈ 12 小时） |
| `deep_scan_enabled` | `true` | 是否允许引用图扫描（后台轮转 + 手动按钮） |
| `deep_scan_interval_samples` | `5` | 每几个采样点跑一轮；`0` 表示只在手动触发时扫 |
| `deep_scan_slice_ms` | `15` | 单个时间切片上限，切完就让出控制权 |
| `deep_scan_duty_percent` | `25` | 扫描最多占用的墙钟比例；25 = 工作 15 ms、休息 45 ms |
| `deep_scan_max_objects` | `120000` | 每插件对象上限 |
| `deep_scan_max_objects_total` | `400000` | 单轮总对象上限 |
| `deep_scan_time_budget_ms` | `3000` | 单轮 CPU 时间预算（不含让出的休息时间） |
| `measure_import_cost` | `true` | 是否在启动窗口记录首次导入 RSS 差分；完整覆盖需要重启 AstrBot |
| `import_hook_max_overhead_ms` | `5000` | 导入包装器自身开销预算，超出即自动降级；`0` 为不限制 |
| `dep_audit_enabled` | `true` | 是否允许依赖审计（纯读源码） |
| `dep_audit_max_files` | `400` | 单次审计最多扫描的 Python 文件数 |
| `dep_audit_time_budget_ms` | `4000` | 审计时间预算；扫不完时再跑一次会接着扫 |
| `census_enabled` | `false` | 是否让后台采样执行对象普查；小 VPS 建议保持关闭 |
| `census_sample_rate` | `10` | 每 N 个 GC 对象取一个再放大，`1` 为全量 |
| `census_time_budget_ms` | `4000` | 对象普查时间预算 |
| `include_object_count` | `false` | 是否额外统计全局 GC 对象数；大进程建议关闭 |
| `alert_rss_mb` | `0` | 进程 RSS 阈值，`0` 为关闭；小 VPS 优先配置 |
| `alert_rss_growth_mb_per_hour` | `0` | RSS 增长阈值，`0` 为关闭 |
| `alert_plugin_mb` | `0` | 单插件对象普查体量阈值，`0` 为关闭 |
| `alert_growth_mb_per_hour` | `0` | 单插件对象增长阈值，`0` 为关闭 |
| `persist_history` | `true` | 是否把最近历史写入插件 KV |
| `command_top_n` | `8` | `/mem` 默认显示数量，范围 1～30 |
| `registry_ttl_seconds` | `30` | 插件清单刷新间隔；通常保持默认 |

告警对同一目标/类型默认有 30 分钟冷却，避免刷屏。

## 小内存 VPS 建议

1. 先把 `alert_rss_mb` 配成物理内存的约 60%～70%，再观察 `alert_rss_growth_mb_per_hour`。
2. 保持 `proc_smaps_enabled=true`：在会 swap 的机器上，只看 RSS 会低估一大截。
3. 保持 `census_enabled=false`；排查时手动跑一次，不要把它设成高频后台任务。
4. 核心紧张时把 `deep_scan_duty_percent` 调到 20，或把 `deep_scan_interval_samples` 调大；机器宽裕可以调到 50 以上换更快出结果。
5. 看到已换出持续上升时先处理整体进程压力，不要等插件数字“精确分摊”。
6. 导入成本只在完整重启时最有意义；运行时安装/重载只做短窗口观察，随后自动释放包装器。

## 目录结构

```text
astrbot_plugin_memory_scope/
├─ main.py                    插件入口、生命周期、采样循环和 /mem 命令
├─ core/
│  ├─ proc_memory.py          smaps_rollup 解析与带缓存的读取器
│  ├─ import_cost.py          短时导入 RSS 账本
│  ├─ dep_audit.py            顶层依赖 AST 审计
│  ├─ object_census.py        GC 对象归属普查
│  ├─ retained_scan.py        引用图保留量：公平配额、轮转、切片让出
│  ├─ plugin_registry.py      插件清单和路径解析
│  ├─ sampler.py              历史、基线、趋势和告警
│  ├─ collector.py            统一报告、进程计数与深扫调度
│  ├─ web_api.py              Dashboard API
│  └─ text_report.py          命令文本输出
├─ pages/memory/              Dashboard 原生 HTML/CSS/JS 页面
├─ .astrbot-plugin/i18n/      zh-CN / en-US 词典
└─ tests/                     pytest 单元测试
```

## 开发与验证

```bash
python -m py_compile core/*.py main.py
python -m pytest tests -q
```

测试不需要真实启动 AstrBot。覆盖范围包括 `smaps_rollup` 解析与降级、导入包装器、插件目录映射、对象普查、引用图扫描的配额与轮转、配置边界与 clamp、历史迁移与 payload 版本、文本报告、Web API 契约，以及前端资产的静态约束（HTML id 与 `el()` 调用一致、i18n 键零漂移零孤儿）。

## 已知限制

- 不能精确计算每个插件独占 RSS；共享 Python 模块、native 缓冲区、线程栈和分配器 arena 没有可靠的插件标签。
- 归因覆盖率永远小于 100%，因为解释器内部结构和 C 扩展 arena 从 Python 对象图不可达。
- 足迹依赖 `/proc/self/smaps_rollup`；旧内核、非 Linux 或该文件不可读时只能看 RSS，页面会明确标注来源已退化。
- 引用图扫描永远是**下界**：有对象配额、时间配额和 denylist，本轮扫不完的插件下一轮才补上。
- 对象普查只看到 GC 跟踪对象，`str`、`bytes`、很多整数和 C 扩展内部缓冲区可能完全不出现；`sys.getsizeof` 也只是浅层大小。
- 导入成本受加载顺序影响，只覆盖包装器安装之后首次加载的插件；共享依赖只记给第一个导入者。
- `memory_percent` 是相对整台系统的比例；容器环境应优先看 cgroup 字段（如果宿主机提供）。
- MemoryScope 自身也出现在插件清单里，标记为“本插件”，它的开销同样可见。

## License

MIT © Whereis-Alice
