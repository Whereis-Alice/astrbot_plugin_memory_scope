# MemoryScope

按插件维度统计 AstrBot 的内存占用，并在 Dashboard 里可视化：谁占得多、谁在持续增长、增长发生在哪个文件哪一行。

- 归因来源：tracemalloc（分配归因）+ gc 引用图扫描（保留量估算）+ psutil（进程级 RSS）
- 交互入口：Dashboard 插件页面 **MemoryScope** 与聊天命令 `/mem`
- 采样常驻后台（默认 60 秒一次），历史写入插件 KV，重载不丢曲线
- **不给启动过程添乱**：tracemalloc 默认不自动开启，开启也只发生在 AstrBot 全部插件加载完成之后（见「为什么默认不自动开启追踪」）

## 为什么需要三层指标

单一数字无法回答"这个插件占了多少内存"，MemoryScope 同时给出三类互补口径：

| 指标 | 字段 | 来源 | 含义 | 局限 |
| --- | --- | --- | --- | --- |
| 归因分配 | `attributed_bytes` | tracemalloc | 把每个存活内存块沿调用栈**由内向外**找到第一个属于插件目录的帧，记在该插件账上。插件调用 `json.loads` 产生的内存算插件的，不算标准库的 | 只统计 tracemalloc 启动之后的分配 |
| 直接分配 | `direct_bytes` | tracemalloc | 分配点本身就在插件源码里（最内层帧即插件文件） | 同上 |
| 独占保留 | `retained.exclusive_bytes` | gc 引用图 | 从插件实例与其模块出发遍历引用图，只有该插件可达的对象大小之和 | 估算值，需手动触发深度扫描 |
| 共享保留 | `retained.shared_bytes` | gc 引用图 | 多个插件都能到达的对象，单独列出，避免重复计数 | 同上 |
| 基线增量 | `delta_bytes` | 采样历史 | 相对用户设定基线的变化量 | 需先设基线 |
| 增长趋势 | `trend_bytes_per_minute` | 采样历史 | 最近 20 个采样点的最小二乘斜率（字节/分钟），UI 按每小时展示 | 至少需要 3 个采样点 |

> 归因分配不等于"现在占用"：一个插件可能分配了内存后交给核心持有，也可能早已释放。判断泄漏请看**趋势**与**独占保留**，判断瞬时体量看**归因分配**。

## 安装

克隆到 AstrBot 的插件目录后重载插件：

```bash
cd data/plugins
git clone https://github.com/Whereis-Alice/astrbot_plugin_memory_scope
```

依赖只有 `psutil`（AstrBot 自带）。缺失时插件仍可运行，只是没有进程级 RSS/系统内存数据，页面会提示 `psutil_missing`。

## Dashboard 页面

仪表盘 → 插件 → MemoryScope。页面提供：

- **概览卡**：进程 RSS / 系统占比、tracemalloc 当前与峰值（可重置峰值）、插件归因合计、GC 代计数与不可回收对象
- **Top 10 条形图** 与非插件部分的分桶（AstrBot 核心、标准库、各第三方库）
- **插件表**：10 列，表头可排序（空值恒排最后）、名称搜索、每行 sparkline 走势
- **详情抽屉**：点击任意行（或回车）查看元信息、各口径数值、历史折线、分配热点行号、已加载子模块
- **操作**：开关 tracemalloc、重置峰值、设置/清除基线、手动 GC、深度扫描开关、自动刷新（10/30/60/300 秒）、深浅色主题
- **告警面板**：超过体量或增长阈值时在页面与日志中提示

页面接口挂在 `/api/plug/astrbot_plugin_memory_scope/*` 下，沿用 Dashboard 自身的 JWT 鉴权，插件不额外开放未认证端点。

## 命令

命令组 `/mem`（别名 `/memoryscope`），**全部需要管理员权限**：

| 命令 | 作用 |
| --- | --- |
| `/mem top [N]` | 占用最高的 N 个插件（默认取配置 `command_top_n`） |
| `/mem deep [N]` | 执行一次引用图深度扫描后输出，附耗时与是否被截断 |
| `/mem plugin <名称>` | 单个插件明细：各口径数值 + 分配热点行号（名称支持唯一子串匹配） |
| `/mem gc` | 手动 GC，报告回收对象数与释放字节 |
| `/mem trace on` / `off` / `peak` | 开启 / 关闭 tracemalloc、重置峰值 |
| `/mem base set` / `clear` | 设置 / 清除对比基线 |

## 配置项

在插件管理页填写，改动即时生效（无需重启）：

| 键 | 默认 | 取值范围 | 说明 |
| --- | --- | --- | --- |
| `auto_start_tracemalloc` | **false** | - | 是否在 AstrBot 加载完成后自动开启 tracemalloc。默认关闭，按需在页面或 `/mem trace on` 开启 |
| `auto_start_min_available_mb` | 512 | >=0 | 自动开启前的内存闸门：系统可用内存低于该值就不开，只写一条 WARNING。0 = 不检查 |
| `tracemalloc_frames` | 12 | 1-40 | 调用栈保留帧数。太小会导致深层调用归因不到插件，太大更耗内存 |
| `sample_interval_seconds` | 60 | 10-3600 | 后台采样间隔 |
| `history_size` | 720 | 30-5000 | 内存中保留的采样点数量 |
| `deep_scan_enabled` | true | - | 是否允许引用图深度扫描 |
| `deep_scan_max_objects` | 120000 | 5000-2000000 | 单个插件遍历对象上限 |
| `deep_scan_max_objects_total` | 400000 | 10000-8000000 | 单次扫描全局对象上限 |
| `deep_scan_time_budget_ms` | 3000 | 200-60000 | 单次扫描时间预算，超时即截断 |
| `include_object_count` | false | - | 附带 `gc.get_objects()` 总对象数（本身有开销） |
| `alert_plugin_mb` | 0 | >=0 | 单插件归因内存超过该 MB 值告警，0 关闭 |
| `alert_growth_mb_per_hour` | 0 | >=0 | 增长速度超过该 MB/小时告警，0 关闭 |
| `persist_history` | true | - | 采样历史写入插件 KV（每 5 次采样落盘，保留最近 240 条） |
| `command_top_n` | 8 | 1-30 | 命令默认输出条数 |

告警有 30 分钟的同类冷却，不会刷屏。

## 为什么默认不自动开启追踪

v1.0.0 在插件构造函数里就 `tracemalloc.start(12)`，本意是尽量覆盖后续插件的导入过程。实测代价远超收益：

在一台 2 核 / 1.6 GB 内存、装了 123 个插件的 AstrBot 上，MemoryScope 排在第 18 个加载，追踪从那一刻开启：

| 分组 | 本次启动实测 | 无追踪时的基线 | 倍数 |
| --- | --- | --- | --- |
| 追踪开启**前**加载的 17 个插件 | 4 s | 4.7 s | 0.85x |
| 追踪开启**后**加载的 104 个插件 | 1259 s | 34.5 s | **36x** |

整体表现为：插件加载阶段 39 s → **1265 s（21 分钟）**，Dashboard 端口 25 分钟后仍未 listen，swap 用量从 2.0 GB 涨到 3.3 GB，运维会以为 AstrBot 启动失败。原因是 tracemalloc 会给**每一次内存分配**记录一条最多 12 帧的调用栈：import 阶段正是分配最密集的时候，既拖慢分配路径，又把追踪表本身撑大，在小内存机器上直接触发换页雪崩。

v1.0.1 的处理：

1. 构造函数不再碰 tracemalloc，`auto_start_tracemalloc` 默认 **false**；
2. 即使置为 true，也只在 `on_astrbot_loaded` 钩子（所有插件加载完、Dashboard 已起）里开启，启动路径零影响；
3. 新增 `auto_start_min_available_mb`（默认 512 MB）内存闸门，机器本来就快满时拒绝自动开启并写明原因；
4. 后台采样循环同样等 `on_astrbot_loaded` 之后才跑第一次，避免快照撞上 import 高峰；
5. 单次采样耗时超过采样间隔 20% 时，间隔自动临时放大（最多 8 倍）并写一条 WARNING。

想要覆盖导入期的完整数据，正确做法始终是 `PYTHONTRACEMALLOC`（见下一节）：它由解释器自己在启动时开启，不存在"中途插入"的放大效应。

## 关于 tracemalloc 的重要限制

**tracemalloc 只能看到它启动之后发生的分配。** 如果由本插件在加载时启动，那么所有插件（包括先于它加载的）在 import 阶段分配的内存都不在统计内，归因合计会明显小于实际。此时页面顶部会显示黄条，报告里带 `tracing_started_late` 标记。

要获得覆盖导入期的完整数据，用环境变量在解释器启动时就打开追踪：

```powershell
$env:PYTHONTRACEMALLOC=12
python main.py
```

```bash
PYTHONTRACEMALLOC=12 python main.py
```

Docker Compose：

```yaml
services:
  astrbot:
    environment:
      - PYTHONTRACEMALLOC=12
```

数值即帧数，建议与 `tracemalloc_frames` 保持一致。检测到追踪由外部开启时，插件不会在卸载时关掉它，避免打断运维自己的 profiling。

## 开销

| 动作 | 开销 |
| --- | --- |
| tracemalloc 常开（12 帧） | 额外内存约为进程的 10%-30%；稳态分配路径损耗不大，但在 import 高峰期实测可放大到 36 倍（见上文），所以插件绝不在加载阶段开启 |
| 常规采样（默认 60 秒） | 一次快照统计，通常数十毫秒 |
| 引用图深度扫描 | 数百毫秒到数秒，受 `deep_scan_*` 上限约束；仅在请求 `deep=1` 或 `/mem deep` 时执行 |
| 手动 GC | 与进程对象数相关，通常几十到几百毫秒 |

深度扫描不是常驻行为：页面在非深度刷新时复用上一次结果，并在角标里标明数据时间。如果不需要保留量口径，可以关掉 `deep_scan_enabled`。

## 目录结构

```text
astrbot_plugin_memory_scope/
├─ main.py                    插件入口：延后开启追踪、采样循环、KV 持久化、/mem 命令
├─ core/
│  ├─ plugin_registry.py      插件清单与"文件路径 -> 插件"解析
│  ├─ tracemalloc_probe.py    tracemalloc 生命周期与归因算法
│  ├─ retained_scan.py        引用图扫描（独占/共享、denylist、预算）
│  ├─ sampler.py              采样历史、基线、趋势、告警引擎
│  ├─ collector.py            汇总为统一 payload
│  ├─ web_api.py              Dashboard 接口
│  └─ text_report.py          命令行文本渲染
├─ pages/memory/              Dashboard 页面（原生 HTML/CSS/JS）
└─ tests/                     pytest 单元测试
```

## 开发

```bash
python -m pytest astrbot_plugin_memory_scope/tests -q
```

测试只依赖 `core/*` 子模块，不需要真实运行 AstrBot。

## 已知限制

- 引用图扫描是**估算**：模块、类、函数、协程等边界类型只计自身大小、不再深入；AstrBot `Context`、事件循环、logging 树在 denylist 中被跳过，因此结果偏保守。
- 被截断的行会标注 `truncated`，此时数值偏小，可提高 `deep_scan_time_budget_ms` 后重试。
- MemoryScope 自己也在表中（标记 `self`），它的开销同样可见。
- `memory_percent` 基于 RSS 与系统总内存，容器里可能与 cgroup 限额不一致。

## License

MIT © Whereis-Alice
