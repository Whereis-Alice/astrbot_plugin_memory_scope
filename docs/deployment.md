# 部署与接入

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

插件页面的顶部导航提供总览、插件、启动记录、诊断、对照和说明；独立页面使用相同布局。

连接后端时，插件只周期性同步插件清单；持续采样和历史交给后端，引用图及对象普查按需执行。普通页面刷新不会新增本地采样。

日常填写方法见[普通用户配置指南](configuration.md)。特别注意：**本地导入统计和自动对象普查在后端模式下都会停用，即使配置框仍显示勾选也不自动运行。** 早期启动探针由服务器启动入口控制，手动对象普查仍可使用；普查不是提高全部插件内存准确率的开关。

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


## 回退

后端不可达时，页面显示错误或带时间的过期数据。启动配置无法读取时，入口直接运行 AstrBot，不启用集成。加载器不兼容时探针撤回，报告标为不完整。

恢复原启动方式：备份并移走本工具创建的 `90-memoryscope.conf`，执行 `systemctl daemon-reload`，再重启 AstrBot。不要使用可能移除其他 drop-in 的 `systemctl revert`。历史保留在 `/var/lib/memoryscope`。

后端日志：`journalctl -u memoryscope`。
