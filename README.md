# MemoryScope 4

AstrBot 的独立内存观察与启动诊断工具：**服务器采集器 + 可选启动探针 + AstrBot 展示插件**。机器人重启、卡住或退出时，采集器仍能保存记录；A/B/A 对照用于验证停用插件的实际条件收益。

**不承诺“逐插件独占 RSS”的精确账单。** 内核给进程和 cgroup 记账，不给 Python 插件记账。启动增量、对象估算、共享依赖和对照收益是不同证据，不能混加。

## 功能与口径

| 功能 | 来源 | 边界 |
| --- | --- | --- |
| 服务总量、趋势、内存压力 | cgroup v2、`/proc` | 包括服务子进程，区分物理记账、匿名内存、文件页与 Swap |
| 子进程明细 | PID＋内核启动时间 | RSS / Swap / PSS；短命进程可能落在采样间隙里 |
| 启动阶段 | 首个插件前安装的探针 | 分开导入、构造、初始化；增量仍受并发任务影响 |
| 无探针的启动观察 | journal 加载标记 | 按日志接收时间估算窗口；没有结束边界的末项保持未知 |
| 共享依赖 | 启动时新出现的包 | 记录首次出现位置，不虚构大小、不重复分摊 |
| A/B/A 重启对照 | 多次独立启动 | 普通禁用与完全跳过导入分别测量，报告差异和波动 |
| 进程内诊断 | 插件手动扫描 | 保留依赖审计、对象普查、引用图扫描、手动 GC |
| 独立报告页 | 后端 HTTP API | AstrBot 无法启动时也能查看，可导出 JSON |

`anon + Swap` 包含换出量，**不是物理内存占用**。PSS 按进程分摊共享页，不能拆出每个插件的独占页。cgroup 已包含子进程，不能再加一次子进程 RSS。

## 部署

后端支持 Linux / cgroup v2 / Python **3.11+**，只使用标准库。systemd 部署支持自动重启对照。

在仓库目录执行（按实际路径调整）：

```sh
sudo python3 tools/install_observer.py \
  --astrbot-root /root/AstrBot \
  --astrbot-python /root/AstrBot/.venv/bin/python \
  --unit astrbot.service --enable-probe
```

安装位置：

- 程序：`/opt/memoryscope`
- 配置：`/etc/memoryscope/observer.json`，随机访问凭据只保存在文件中
- 历史与实验状态：`/var/lib/memoryscope`
- 独立服务：`memoryscope.service`
- 可选启动接入：`astrbot.service.d/90-memoryscope.conf`
- 受管理文件的备份：`/root/memoryscope-backups/<时间>`

安装器**不会重启 AstrBot，不修改 AstrBot 源码、数据库或业务配置**。安装器适配原启动命令为 `python main.py` 的部署；自定义入口、额外启动参数需先调整 drop-in。原 service 文件保留。

准备好后执行一次 `sudo systemctl restart astrbot`。省略 `--enable-probe` 可只部署外部采集。

采集器必须处于独立 cgroup，避免把自身占用计入 AstrBot。

## 插件接入

在 AstrBot 插件管理中安装本仓库。观测启动入口会通过环境变量传递配置路径，插件自动发现后端。

不使用启动入口时，可在插件配置中填写：

```text
observer_config_path = /etc/memoryscope/observer.json
```

或设置 `observer_url` 与 `observer_token`。配置文件应授权给采集器及 AstrBot 运行用户读取。

插件页面的 **服务器观察** 提供总览、启动报告、对照实验和口径说明。原来的进程内诊断页面保留。

连接后端时，插件只周期性同步插件清单；持续采样和历史交给后端，引用图及对象普查按需执行。普通页面刷新不会新增本地采样。

| 管理员命令 | 功能 |
| --- | --- |
| `/mem top` | 外部总览；未配置后端时显示本地总览 |
| `/mem startup` | 最近一次启动阶段报告 |
| `/mem imports` | 后端模式显示启动报告，本地模式显示旧导入账本 |
| `/mem plugin <名称>` | 本地诊断与当前批次的启动阶段 |
| `/mem compare` | 最近的对照实验结果 |
| `/mem audit` | 手动重依赖源码审计 |
| `/mem census` | 手动对象普查 |
| `/mem deep` | 手动引用图扫描 |
| `/mem gc` | 手动 GC，不保证 RSS 下降 |
| `/mem base set`、`/mem base clear` | 本地趋势基线 |

## AstrBot 离线时查看

后端默认只监听 `127.0.0.1:8766`；探针事件使用本机 UDP `8767`。HTTP API 使用 Bearer token。

```sh
ssh -L 8766:127.0.0.1:8766 your-user@your-server
```

浏览器打开 `http://127.0.0.1:8766`，输入后端配置文件的 token。凭据只保存在当前页面内存。

AstrBot 内嵌页面继承 Dashboard 登录态，token 留在插件服务端。采集器不记录进程命令行参数、环境变量或消息正文；环境配置只保存哈希指纹。

## 对照实验

后端配置 `allow_experiments` 默认为 `false`。管理员启用并重启 **memoryscope 服务**后，页面可创建实验，并明确确认重启次数。

1 轮：**A → B → A**，共 3 次重启。2 轮：A → B → A → B → A，共 5 次。默认每次端口就绪后等待 60 秒，再采集 30 秒的稳定窗口。

- `disable`：仅在该次启动中返回临时禁用名单；模块仍可能被导入。
- `skip`：该次启动从插件发现结果中排除目标，避免直接导入；其他代码仍可能间接导入目标或共享依赖。

方案只使用一次并带有效期，保存在采集器目录。**不改持久禁用名单、不移动插件、不删除业务数据。** 保留插件和观察插件自身不能被自动排除。

任务由后端执行。取消、失败后尝试恢复正常启动；恢复失败会明确记录。后端崩溃后不续跑未完成实验，识别到仍处于 B 配置时执行正常启动恢复。

报告检查环境指纹、窗口完整性、重复次数和自然波动。单轮属于初步结果。消息量、外部请求与后台任务仍需保持可比；不同插件的节省量不能相加当作可以同时释放的总量。

## 开销与限制

- 常态默认每 5 秒采样。进程明细最小间隔 2 秒，PSS 最小间隔 30 秒；实际频率不会超过总体采样频率。
- 启动期间默认 100 ms 采样，最长 180 秒；就绪后短暂继续，随后降频。
- 探针不使用 tracemalloc，不逐次追踪分配；启动完成后恢复包装的方法与导入查找器。
- 事件发送非阻塞，带预算和序号。丢包会影响完整性，不能阻塞 Bot 等待后端。
- 对象扫描仍在 AstrBot 进程内，可能占 CPU、触发换入及停顿。4.0 默认自动扫描间隔为 0，连接后端时强制按需。
- 历史最多 7 天，采样与事件分别最多 50000 条，同时受容量限制；压缩保存并定期清理。观察器服务另有 192 MiB 内存限额。

[服务器实测与验证记录](docs/performance-20260918.md)：候选接入版三次启动中位数增加约 5.91 秒（4.48%）；最终版单次复核为 136.91 秒。96 秒常态观察中，后端 RSS 约 29.3 MiB，含辅助进程的 CPU 约单核 0.52%。不是零开销，也不把短时观测当成长期保证。

探针“自身耗时”只覆盖部分直接工作，不等于完整启动开销。性能结论以目标服务器重复实测为准：

```sh
# 此命令真的重启 AstrBot 三次。
python3 tools/benchmark_service.py --config /etc/memoryscope/observer.json \
  --label baseline --runs 3 --settle 20 --confirm-restart --output baseline.json
```

配置 `cgroup` 可观察 Docker 容器的 cgroup v2 路径，采集器运行在宿主机；容器重建后可能需要更新路径。**自动重启实验仅支持 systemd**。容器内部的早期探针涉及 PID 命名空间与事件通道，本版未自动适配。

## 回退

后端不可达时，页面显示错误或带时间的过期数据。启动配置无法读取时，入口直接运行 AstrBot，不启用集成。加载器不兼容时探针撤回，报告标为不完整。

恢复原启动方式：备份并移走本工具创建的 `90-memoryscope.conf`，执行 `systemctl daemon-reload`，再重启 AstrBot。不要使用可能移除其他 drop-in 的 `systemctl revert`。历史保留在 `/var/lib/memoryscope`。

后端日志：`journalctl -u memoryscope`。

## 开发

```sh
python -m pytest -q
python -m ruff check observer core/observer_client.py core/observer_api.py tools tests/test_observer.py
node --check pages/memory/observer.js
```

`observer/`：独立后端与探针；`core/observer_*`：插件桥接；`pages/memory/observer.*`：两端共用页面；`tools/`：部署与实测工具。许可证：MIT。
