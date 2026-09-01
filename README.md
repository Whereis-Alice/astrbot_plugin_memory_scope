# MemoryScope

MemoryScope 是一个面向小内存 VPS 的 AstrBot 内存观察插件。它不承诺一个不存在的“每个插件独占 RSS”精确数字，而是把问题拆成几种可验证、开销不同的口径：

- **进程 RSS / swap / cgroup**：操作系统真正记账的总量，默认持续采样；
- **加载期导入成本**：只在启动加载窗口短暂包裹 `builtins.__import__`，用 RSS 前后差找出大插件和大依赖；
- **对象归属普查**：用户手动触发后，按对象类型所属模块估算存活 Python 对象；
- **引用图保留量**：用户手动触发后，从插件实例和模块向外走，估算仍被插件保持可达的对象；
- **顶层依赖审计**：只读 AST 源码，找出可以改成惰性导入的重依赖。

页面入口是 Dashboard 插件页中的 **MemoryScope**，聊天入口是管理员命令组 `/mem`。

## 先说结论：不能精确算“插件独占 RSS”

Linux/Windows 的 RSS 是进程级页集合，不带“这 4 KB 属于哪个 Python 插件”的标签。多个插件共享同一份解释器、第三方库、内存分配器 arena、线程栈和 C 扩展缓冲区时，任何插件都不能仅靠一个轻量 Python 插件把它们准确拆开。

因此页面会明确区分：

| 口径 | 能回答什么 | 不能回答什么 | 默认行为 |
| --- | --- | --- | --- |
| 进程 RSS | AstrBot 此刻占了多少驻留内存 | 哪个插件独占这些页 | 每次采样读取一次 `/proc/self/statm` 或 psutil |
| 加载期导入成本 | 某插件首次加载大约让 RSS 增加多少 | 该插件现在仍保留多少；共享包不能重复分摊 | 启动期短暂启用，加载完成即卸载 |
| 对象普查 | 哪些插件模块拥有较多 GC 跟踪对象 | C 缓冲区、字符串/bytes 等未跟踪对象 | 关闭，按钮或 `/mem census` 手动执行 |
| 引用图保留量 | 从插件根对象可达的独占/共享对象下界 | 完整 native 内存和所有间接拥有关系 | 关闭常驻，只手动执行 |
| 趋势与基线 | RSS 或对象数量是否持续增长 | 增长的根因 | 默认记录 RSS；对象曲线只使用普查样本 |

如果目标是判断小鸡会不会被打爆，**先看 RSS、swap 和 RSS 增长趋势**，不要把对象普查或导入账本的数字加起来当作进程总内存。

## 为什么彻底移除了 tracemalloc

早期 v1.0.0 在插件构造函数里直接执行 `tracemalloc.start(12)`。这会让此后的每一次 Python 内存分配都记录最多 12 帧调用栈；插件导入阶段正是分配最密集的阶段，在小内存机器上会迅速进入换页雪崩。

真实事故数据（2 核 / 约 1.6 GB 内存、123 个插件）：

| 分组 | 有追踪本次实测 | 无追踪基线 | 倍数 |
| --- | ---: | ---: | ---: |
| 追踪开启前的 17 个插件 | 4 s | 4.7 s | 0.85× |
| 追踪开启后的 104 个插件 | 1259 s | 34.5 s | **约 36×** |
| 整体插件加载 | 39 s | — | **1265 s（约 21 分钟）** |

期间 Dashboard 端口约 25 分钟没有 listen，swap 从约 2.0 GB 增至 3.3 GB。这个代价与“插件独占内存并不精确”的收益不匹配，所以 v2 **不导入、不调用、不在页面提供开启入口**。后台也不做常驻全堆扫描。

## v2 的轻量实现

### 1. 进程级计数（默认常驻）

普通采样只读取一次进程 RSS，Linux 上同时读取 `/proc/self/status` 的匿名页、文件页、共享内存和 swap，并尽可能读取 cgroup 当前用量与上限。读取不会遍历 Python 对象，也不会改变 Python 分配路径。

### 2. 加载期导入账本（默认开启，但只活在启动窗口）

MemoryScope 尽早安装一个很小的 `__import__` 包装器，仅对第一次打开的插件/顶层包读取 RSS 差和耗时。AstrBot 完成插件加载后立即卸载；运行时从 Dashboard 安装或重载插件时，最多保留 8 秒窗口，随后自动释放包装器。

它的作用是找“启动时谁拉进了大包”，不是持续监控。共享包只记在第一个导入者名下，避免虚假的重复占用；这也意味着不能把某一行的数字理解成插件当前独占 RSS。

### 3. 对象普查（默认关闭）

手动执行 `gc.get_objects()`，按照 `type(obj).__module__` 映射回插件目录，统计对象数量和 `sys.getsizeof` 浅层大小。支持 1/N 抽样和时间预算；抽样结果会标记为估算，超时结果会标记为下界。

### 4. 引用图深扫（默认只允许手动）

从插件实例、主模块和已加载子模块出发，用 `gc.get_referents` 做限额遍历，分开显示仅一个插件可达的对象和多个插件共享的对象。核心 Context、事件循环和 logging 树会被排除，避免把整个 AstrBot 算到某个插件头上。

### 5. 静态依赖审计

AST 只扫描源码模块顶层的 import，不执行插件代码。它会把已测得的包成本、共享插件数和“全部改为函数内导入后理论上可省”的估算放在一起，覆盖范围不受插件加载顺序影响。

## 已知环境实测

以下是同一台约 129 个插件的小鸡上的观测样本，用于解释数字的量级，不是所有机器的保证值：

| 指标 | 观测值 |
| --- | ---: |
| 活跃进程 RSS | 约 790 MB |
| Anonymous | 约 772 MB |
| Pss/File（文件映射近似） | 约 16 MB |
| 已换出 | 约 130 MB |
| VmSize | 约 3049 MB |
| 线程数 | 31 |
| 新鲜进程首次 `import astrbot.api` 的 RSS 增量 | 约 155.4 MB（Windows 约 198 MB） |

`import astrbot.api` 的 155/198 MB **不是 MemoryScope 插件凭空制造的**。它是一次冷启动导入的进程 RSS 增量，包含 AstrBot API 模块继续拉入的框架模块、依赖、类定义、缓存以及 Python 分配器保留的 arena；在已有 AstrBot 进程里再次 import 通常只是命中 `sys.modules`，不会再次付出同样的成本。要判断插件责任，应看插件自己的首次加载成本和它是否把大依赖放在模块顶层，而不是把整个 `astrbot.api` 包的冷导入成本算给插件。

导入账本中观察到的主要第三方包群包括：google 约 36.2 MB、sqlalchemy 约 19.2 MB、mcp 约 18.5 MB、anthropic 约 13.9 MB、openai 约 9.9 MB、aiohttp 约 8.4 MB、pypdf 约 6.2 MB、sqlmodel 约 6.2 MB。插件依赖的边际成本约 240.5 MB / 81 个包；pypinyin、faiss-cpu、sympy、pymupdf 等是更值得优先审计的大户。导入账本约 395 MB，而实际 RSS 约 790 MB，剩余部分来自插件实例、缓存、连接、线程栈、解释器和 native 分配等，正是不能简单按插件平分的部分。

## 开销控制

| 操作 | 典型开销 | 是否后台执行 |
| --- | ---: | --- |
| RSS 读取 | 约数微秒 | 是，每个采样点一次 |
| 导入包装器 | 每次调用约 1～2 微秒；启动总量通常几十毫秒 | 仅插件加载窗口 |
| `gc.get_objects()` 普查 | 约 60 万对象时几十毫秒级；会创建临时列表并触碰换出页 | 否，默认关闭 |
| 引用图深扫 | 数百毫秒到数秒，受对象数/时间预算限制 | 否，默认手动 |
| 手动 GC | 通常几十到几百毫秒；RSS 不一定下降 | 否 |

普通刷新不会打开全堆扫描。小内存 VPS 建议保持 `census_enabled=false`，把 `deep_scan_enabled` 仅理解为“允许手动按钮”，并优先设置进程 RSS 告警。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/Whereis-Alice/astrbot_plugin_memory_scope
```

然后在 AstrBot 插件管理页启用/重载。依赖只有 AstrBot 通常已经带有的 `psutil`；缺失时插件会降级，仍可提供能读取到的轻量数据。

## Dashboard 页面

页面分为：

- **总览**：RSS、匿名页、文件页、swap、cgroup、GC、RSS 曲线、基线和告警状态；
- **插件**：导入成本、对象普查、引用图保留量、趋势和可点击详情；未知值显示为“未知”，不会伪装成 0；
- **导入成本**：按包和按插件查看启动期 RSS 差分；
- **依赖审计**：查看模块顶层重依赖与惰性导入机会；
- **对象普查**：手动运行一次并查看插件对象及其他来源；
- **告警**：进程 RSS、RSS 增长、对象体量和对象增长告警；
- **指标说明**：解释口径与小内存机器建议。

页面接口由 Dashboard bridge 转发，插件不额外开放未认证端点。

## 命令

命令组 `/mem`（别名 `/memoryscope`），全部需要管理员权限：

| 命令 | 作用 |
| --- | --- |
| `/mem top [N]` | 显示进程 RSS 与重点插件（默认显示 `command_top_n` 条） |
| `/mem imports [N]` | 查看加载期包成本和插件加载汇总 |
| `/mem audit [N]` | 扫描模块顶层重依赖 |
| `/mem census [N]` | 手动执行一次对象普查；可能短暂停顿 |
| `/mem deep [N]` | 手动执行一次引用图深扫；结果是下界 |
| `/mem plugin <名称>` | 查看单个插件的导入、对象、引用图和依赖详情 |
| `/mem gc` | 手动触发 GC 并比较 RSS 前后值 |
| `/mem base set\|clear` | 设置或清除 RSS/对象对比基线 |

## 配置项

在插件管理页修改，配置会在插件下一次读取时生效：

| 键 | 默认 | 说明 |
| --- | ---: | --- |
| `measure_import_cost` | `true` | 是否在启动加载窗口记录首次导入 RSS 差分；完整覆盖需要重启 AstrBot |
| `import_hook_max_overhead_ms` | `5000` | 导入包装器自身开销预算；超出后自动降级，`0` 为不限制 |
| `dep_audit_enabled` | `true` | 是否允许依赖审计；审计本身只读源码 |
| `dep_audit_max_files` | `400` | 单次审计最多扫描的 Python 文件数 |
| `dep_audit_time_budget_ms` | `2000` | 依赖审计时间预算 |
| `census_enabled` | `false` | 是否让后台采样执行对象普查；小 VPS 建议关闭 |
| `census_sample_rate` | `10` | 每 N 个 GC 对象取一个并放大估算，`1` 为全量 |
| `census_time_budget_ms` | `4000` | 对象普查时间预算 |
| `sample_interval_seconds` | `60` | RSS 采样间隔，范围 10～3600 |
| `history_size` | `720` | 内存中保留的采样点数 |
| `deep_scan_enabled` | `true` | 是否允许手动引用图扫描 |
| `deep_scan_max_objects` | `120000` | 每插件深扫对象上限 |
| `deep_scan_max_objects_total` | `400000` | 单次深扫总对象上限 |
| `deep_scan_time_budget_ms` | `3000` | 单次深扫时间预算 |
| `include_object_count` | `false` | 深扫时是否额外统计全局 GC 对象数；大进程建议关闭 |
| `alert_rss_mb` | `0` | 进程 RSS 阈值，0 为关闭；小 VPS 优先配置 |
| `alert_rss_growth_mb_per_hour` | `0` | 进程 RSS 增长阈值，0 为关闭 |
| `alert_plugin_mb` | `0` | 单插件对象普查体量阈值，0 为关闭 |
| `alert_growth_mb_per_hour` | `0` | 单插件对象增长阈值，0 为关闭 |
| `persist_history` | `true` | 是否将最近历史写入插件 KV |
| `command_top_n` | `8` | `/mem` 默认显示数量，范围 1～30 |
| `registry_ttl_seconds` | `30` | 插件清单刷新间隔；通常保持默认 |

告警同一目标/类型默认有 30 分钟冷却，避免刷屏。

## 小内存 VPS 建议

1. 先配置 `alert_rss_mb` 为物理内存的约 60%～70%，再观察 `alert_rss_growth_mb_per_hour`。
2. 保持 `census_enabled=false`；排查时手动执行一次，不要把它设成高频后台任务。
3. 看到 swap 持续上升时先处理整体进程压力，不要等待插件对象数字“精确分摊”。
4. 插件导入成本只在完整重启时最有意义；运行时安装/重载只做短窗口观察并会自动释放包装器。
5. 任何需要数秒的引用图扫描都应安排在低峰期。

## 目录结构

```text
astrbot_plugin_memory_scope/
├─ main.py                    插件入口、生命周期、采样和 /mem 命令
├─ core/
│  ├─ import_cost.py          短时导入 RSS 账本
│  ├─ dep_audit.py            顶层依赖 AST 审计
│  ├─ object_census.py        GC 对象归属普查
│  ├─ retained_scan.py        引用图保留量估算
│  ├─ plugin_registry.py      插件清单和路径解析
│  ├─ sampler.py              历史、基线、趋势和告警
│  ├─ collector.py            统一报告和进程计数
│  ├─ web_api.py              Dashboard API
│  └─ text_report.py          命令文本输出
├─ pages/memory/              Dashboard 原生 HTML/CSS/JS 页面
└─ tests/                     pytest 单元测试
```

## 开发与验证

```bash
python -m py_compile core/*.py main.py
python -m pytest tests -q
```

测试不需要真实启动 AstrBot；它覆盖导入包装器、插件目录映射、对象普查、配置边界、历史迁移、报告和 Web API。

## 已知限制

- 不能精确计算每个插件独占 RSS；共享 Python 模块、native 缓冲区、线程栈和分配器 arena 没有可靠插件标签。
- 对象普查只看到 GC 跟踪对象，`str`、`bytes`、很多整数对象和 C 扩展内部缓冲区可能不出现；`sys.getsizeof` 还是浅层大小。
- 抽样、超时和引用图 denylist 都会让结果偏保守；页面会标记估算或截断状态。
- 导入成本受加载顺序影响，只能覆盖导入包装器安装之后首次加载的插件；共享依赖只记首个导入者。
- `memory_percent` 是相对整台系统的比例；容器环境应优先看 cgroup 字段（如果宿主机提供）。
- MemoryScope 自身也会出现在插件清单中，标记为“本插件”，其开销同样可见。

## License

MIT © Whereis-Alice
