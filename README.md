# MemoryScope

AstrBot 的内存观察台。看服务器占用如何变化、哪些插件加载成本较高，需要时再做进一步诊断。

- **总览**：服务内存、Swap、子进程与可放大的趋势图。
- **插件**：搜索插件清单，查看启动阶段、依赖和诊断证据。
- **启动记录**：切换历史启动，区分导入、构造和初始化。
- **诊断快照**：后台低频保存轻量依赖审计和引用图结果；对象普查与完整扫描仍按需执行。
- **对照实验**：通过多次启动，比较临时排除插件前后的变化。

内嵌页和独立报告页使用同一套界面，提供松纸、极夜、暮映三种主题，支持手机浏览、中英文和 JSON 导出。

## 怎么开始

1. 在 AstrBot 插件管理中安装：
   `https://github.com/Whereis-Alice/astrbot_plugin_memory_scope`
2. 想查看完整服务器趋势和启动记录，按[部署指南](docs/deployment.md)安装独立后端。支持 Linux、cgroup v2、Python 3.11 及以上；自动重启对照需要 systemd。
3. 插件配置填写 `observer_config_path = /etc/memoryscope/observer.json`。使用观测启动入口时会自动发现，无需重复填地址和 token。
4. 打开插件的 **MemoryScope 页面**，先看「总览」和「插件」。普通查看不会运行对象扫描。

只安装插件也能使用本地诊断，但看不到完整的服务器历史和早于本插件加载的导入记录。配置如何填写见[普通用户配置指南](docs/configuration.md)。

## 数字怎么看

服务总量是实际采样；插件启动增量、对象估算是排查线索，**不是逐插件当前独占内存**。`—` 表示没有测到，不能当成零。页面「说明」中有简单解释。

对象普查和手动引用图扫描可能短时影响消息处理，建议空闲时使用；对照实验会真实重启 AstrBot，需在页面明确确认。日常观察会使用受限的低频诊断快照，不会自动执行对象普查。

## 常用命令

仅管理员可用：`/mem top` 看总览，`/mem startup` 看启动记录，`/mem plugin <名称>` 查插件，`/mem audit` 运行依赖审计。更多命令见[部署与使用](docs/deployment.md)。

## 文档

- [配置怎么填](docs/configuration.md)
- [后端部署、离线查看与回退](docs/deployment.md)
- [WebUI 使用与故障排查](docs/webui.md)
- [测量口径、实验与性能开销](docs/measurement.md)
- [服务器实测记录](docs/performance-20260918.md)
- [更新日志](changelog.md)

MIT License
