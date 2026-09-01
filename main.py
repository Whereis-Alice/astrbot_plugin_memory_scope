"""AstrBot entry point for the MemoryScope plugin.

MemoryScope explains where an AstrBot process spent its memory: what each
plugin cost to import, which heavy third-party packages are pulled in eagerly
at module top level, how live objects are distributed per plugin, and how the
process RSS moves over time.  Results are exposed both on the Dashboard page
(``pages/memory``) and through the ``/mem`` chat commands.

v2.0 removed tracemalloc completely.  Two reasons, both measured (README has
the tables): it is ruinously expensive during the import phase -- v1.0.0 turned
it on in this constructor and stretched plugin loading from 39 s to 21 minutes
on a 2-core / 1.6 GB host -- and its per-plugin numbers are not trustworthy
anyway, because a shared dependency is billed entirely to whichever plugin
happened to import it first.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.collector import MemoryCollector, Settings
from .core.import_cost import get_ledger
from .core.text_report import (
    format_bytes,
    format_ms,
    render_audit,
    render_census,
    render_gc,
    render_imports,
    render_overview,
    render_plugin_detail,
)
from .core.web_api import MemoryScopeWebApi

PLUGIN_ID = "astrbot_plugin_memory_scope"
PLUGIN_VERSION = "2.0.1"
# Key used with the plugin KV store so history survives a reload.
HISTORY_KEY = "history"
# Flush the ring buffer to the KV store every N samples instead of every sample.
PERSIST_EVERY = 5
PERSIST_KEEP = 240
# A sample is now just an RSS read (microseconds), but the first tick is still
# delayed: while AstrBot imports plugins the event loop is blocked, so this
# sleep in practice expires only once the import phase is over.  That is also
# what makes it a reliable "loading finished" signal for the hook handover.
FIRST_SAMPLE_DELAY_SECONDS = 8.0


@register(
    "MemoryScope",
    "Whereis-Alice",
    "解释 AstrBot 的内存去向：插件导入成本、重依赖顶层导入审计、对象归属普查、RSS 趋势与告警",
    PLUGIN_VERSION,
    "https://github.com/Whereis-Alice/astrbot_plugin_memory_scope",
)
class MemoryScopePlugin(Star):
    """Per-plugin memory accounting for AstrBot."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.raw_config = config
        self.settings = Settings.from_config(config or {})
        # Install the import hook before anything else in this constructor: it
        # can only account for plugins AstrBot imports *after* MemoryScope, so
        # every line of setup delayed here is coverage lost.  The hook itself
        # is cheap -- two RSS reads and a clock read per first-time import,
        # ~2 us, no allocation tracing -- and it uninstalls itself once
        # on_astrbot_loaded fires.
        self._hook_installed = False
        if self.settings.measure_import_cost:
            self._hook_installed = self._ledger().install()
        try:
            self.collector = MemoryCollector(context, self.settings, PLUGIN_ID)
            self.web_api = MemoryScopeWebApi(PLUGIN_ID, self.collector)
            self._sampler_task: asyncio.Task[None] | None = None
            self._samples_since_persist = 0
            self._history_loaded = False
            # Set by the on_astrbot_loaded hook, which only fires during AstrBot
            # startup: a runtime install or reload never sees it.
            self._loaded_seen = False

            self._web_routes = self.web_api.register(context)
            if not self._web_routes:
                logger.warning(
                    "MemoryScope 未能注册 Web API，Dashboard 页面将不可用"
                    "（当前 AstrBot 版本可能过旧或缺少 quart）",
                )
        except Exception:
            # AstrBot may report a constructor failure without calling
            # ``terminate``.  Never leave a process-wide import wrapper behind
            # in that case; an orphaned wrapper would defeat the whole point of
            # this plugin being lightweight.
            if self._hook_installed:
                ledger = self._ledger()
                if ledger.installed:
                    ledger.uninstall()
            raise

    def _ledger(self):
        return get_ledger(self.settings.import_hook_max_overhead_ms)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        await self._load_history()
        if self._sampler_task is None or self._sampler_task.done():
            self._sampler_task = asyncio.create_task(self._sampler_loop())
        status = self.collector.import_hook_status()
        logger.info(
            "MemoryScope 已启动 · 导入成本钩子=%s（自身开销 %s）"
            " · 采样间隔 %ss · 依赖审计=%s · 对象普查=%s · 深度扫描=%s",
            self._hook_state(status),
            format_ms(status.get("overhead_ms")),
            self.settings.sample_interval_seconds,
            "on" if self.settings.dep_audit_enabled else "off",
            "on" if self.settings.census_enabled else "off",
            "on" if self.settings.deep_scan_enabled else "off",
        )

    def _hook_state(self, status: dict[str, Any]) -> str:
        if not self.settings.measure_import_cost:
            return "off(配置关闭)"
        if status.get("degraded"):
            return f"degraded({status.get('degraded_reason') or 'unknown'})"
        if status.get("installed"):
            return "on"
        return "done" if status.get("plugin_count") else "off"

    async def terminate(self) -> None:
        task = self._sampler_task
        self._sampler_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._persist_history()
        # Never leave a wrapper on builtins.__import__ behind after a reload.
        ledger = self._ledger()
        if ledger.installed:
            ledger.uninstall()
        self.collector.close()
        logger.info("MemoryScope 已停止")

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        """Loading is over: stop accounting imports and re-read the catalog."""

        self._loaded_seen = True
        ledger = self._ledger()
        if ledger.installed:
            ledger.uninstall()
        self.collector.ensure_registry(force=True)
        status = self.collector.import_hook_status()
        if not status.get("plugin_count"):
            return
        logger.info(
            "MemoryScope 导入成本统计完毕 · %s 个插件 / %s 个第三方包"
            " · 插件自身代码 %s · 第三方依赖 %s · 加载期 RSS 增长 %s"
            " · 钩子自身开销 %s",
            status.get("plugin_count"),
            status.get("package_count"),
            format_bytes(status.get("plugin_bytes")),
            format_bytes(status.get("package_bytes")),
            format_bytes(status.get("rss_growth_bytes")),
            format_ms(status.get("overhead_ms")),
        )

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    async def _load_history(self) -> None:
        if self._history_loaded or not self.settings.persist_history:
            return
        self._history_loaded = True
        try:
            payload = await self.get_kv_data(HISTORY_KEY, None)
        except Exception as exc:  # noqa: BLE001 - KV store is best effort
            logger.debug("MemoryScope 读取历史采样失败: %s", exc)
            return
        if payload:
            self.collector.history.load_payload(payload)
            logger.debug(
                "MemoryScope 已恢复 %s 条历史采样",
                self.collector.history.count(),
            )

    async def _persist_history(self) -> None:
        if not self.settings.persist_history:
            return
        if self.collector.history.count() == 0:
            return
        try:
            await self.put_kv_data(
                HISTORY_KEY,
                self.collector.history.to_payload(PERSIST_KEEP),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("MemoryScope 写入历史采样失败: %s", exc)

    # ------------------------------------------------------------------
    # sampling loop
    # ------------------------------------------------------------------
    async def _sampler_loop(self) -> None:
        await asyncio.sleep(FIRST_SAMPLE_DELAY_SECONDS)
        self._release_orphaned_hook()
        while True:
            interval = max(10, self.settings.sample_interval_seconds)
            try:
                await self._sample_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.warning("MemoryScope 采样失败: %s", exc)
            await asyncio.sleep(interval)

    def _release_orphaned_hook(self) -> None:
        """Uninstall the hook when on_astrbot_loaded is never going to fire.

        A plugin installed, enabled or reloaded from the Dashboard misses that
        hook entirely, and a wrapper left on ``builtins.__import__`` forever is
        exactly the kind of ambient cost this plugin exists to hunt down.
        """

        if self._loaded_seen:
            return
        ledger = self._ledger()
        if not ledger.installed:
            return
        ledger.uninstall()
        self.collector.ensure_registry(force=True)
        logger.info(
            "MemoryScope 按运行时安装/重载处理：导入成本钩子已卸载。"
            "此前已加载的插件无法归因，需要完整的加载期数据请重启 AstrBot。",
        )

    async def _sample_tick(self) -> None:
        for alert in await self.collector.sample_once():
            # sample_once returns only the alerts that passed the cooldown.
            logger.warning("MemoryScope 告警: %s", alert.message)
        self._samples_since_persist += 1
        if self._samples_since_persist >= PERSIST_EVERY:
            self._samples_since_persist = 0
            await self._persist_history()

    # ------------------------------------------------------------------
    # chat commands
    # ------------------------------------------------------------------
    @filter.command_group("mem", alias={"memoryscope"})
    def mem(self):
        """MemoryScope 内存诊断命令。"""

    def _top_n(self, count: int) -> int:
        wanted = count if count and count > 0 else self.settings.command_top_n
        return min(30, max(1, wanted))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("top")
    async def cmd_top(self, event: AstrMessageEvent, count: int = 0):
        """总览：进程 RSS、趋势与各插件的导入成本。"""
        report = await self.collector.build_report(deep=False, record_sample=True)
        yield event.plain_result(render_overview(report, top_n=self._top_n(count)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("imports")
    async def cmd_imports(self, event: AstrMessageEvent, count: int = 0):
        """查看加载期的导入成本：按插件与按第三方包。"""
        report = await self.collector.build_report(deep=False, record_sample=False)
        yield event.plain_result(render_imports(report, top_n=self._top_n(count)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("audit")
    async def cmd_audit(self, event: AstrMessageEvent, count: int = 0):
        """静态扫描插件源码，找出可以改成惰性导入的重依赖。"""
        if not self.settings.dep_audit_enabled:
            yield event.plain_result("依赖审计已在配置中关闭（dep_audit_enabled）。")
            return
        report = await self.collector.audit_now()
        yield event.plain_result(render_audit(report, top_n=self._top_n(count)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("census")
    async def cmd_census(self, event: AstrMessageEvent, count: int = 0):
        """按对象归属做一次全堆普查（进程会停顿）。"""
        yield event.plain_result(
            "正在做对象普查：需要遍历整个 GC 堆，进程会停顿数百毫秒到数秒，"
            "内存紧张的机器还会把换出的页读回内存。请稍候…",
        )
        report = await self.collector.census_now()
        yield event.plain_result(render_census(report, top_n=self._top_n(count)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("deep")
    async def cmd_deep(self, event: AstrMessageEvent, count: int = 0):
        """执行一次引用图深度扫描并查看结果。"""
        if not self.settings.deep_scan_enabled:
            yield event.plain_result("深度扫描已在配置中关闭（deep_scan_enabled）。")
            return
        yield event.plain_result("正在扫描引用图，可能需要几秒…")
        report = await self.collector.build_report(deep=True, record_sample=True)
        meta = report.get("deep_meta") or {}
        text = render_overview(report, top_n=self._top_n(count))
        suffix = f"\n\n深度扫描耗时 {meta.get('elapsed_ms') or 0} ms"
        if meta.get("truncated"):
            suffix += "（已达上限被截断，结果偏小）"
        yield event.plain_result(text + suffix)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("plugin")
    async def cmd_plugin(self, event: AstrMessageEvent, name: str = ""):
        """查看单个插件的明细。"""
        target = (name or "").strip()
        if not target:
            yield event.plain_result("用法：/mem plugin <插件名>，插件名见 /mem top。")
            return
        self.collector.ensure_registry()
        entry = self.collector.registry.get(target)
        if entry is None:
            entry = self._fuzzy_match(target)
        if entry is None:
            yield event.plain_result(f"未找到插件 {target}，请用 /mem top 查看可用名称。")
            return
        report = await self.collector.build_report(
            deep=False,
            detail_for=entry.name,
            record_sample=False,
        )
        yield event.plain_result(render_plugin_detail(report.get("detail") or {}))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("gc")
    async def cmd_gc(self, event: AstrMessageEvent):
        """手动触发一次垃圾回收。"""
        yield event.plain_result(render_gc(await self.collector.force_gc()))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("base")
    async def cmd_base(self, event: AstrMessageEvent, action: str = "set"):
        """设置或清除对比基线：/mem base set|clear。"""
        verb = (action or "set").strip().lower()
        history = self.collector.history
        if verb == "clear":
            history.clear_baseline()
            yield event.plain_result("已清除基线，Δ 列将不再显示。")
            return
        if verb != "set":
            yield event.plain_result("用法：/mem base set | clear")
            return
        if history.latest is None:
            await self.collector.build_report(deep=False, record_sample=True)
        baseline = history.set_baseline()
        if baseline is None:
            yield event.plain_result("暂无采样数据，请稍后重试。")
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(baseline.ts))
        yield event.plain_result(
            f"已将 {stamp} 的采样设为基线，之后 /mem top 会显示相对该时刻的增量。",
        )

    # ------------------------------------------------------------------
    def _fuzzy_match(self, target: str) -> Any:
        needle = target.lower()
        candidates = [
            entry
            for entry in self.collector.registry.entries
            if needle in entry.name.lower()
            or needle in (entry.display_name or "").lower()
        ]
        return candidates[0] if len(candidates) == 1 else None
