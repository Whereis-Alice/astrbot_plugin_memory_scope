"""AstrBot entry point for the MemoryScope plugin.

MemoryScope measures how much memory every installed plugin is responsible for
and exposes the result both in the Dashboard page (``pages/memory``) and through
the ``/mem`` chat commands.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.collector import MemoryCollector, Settings
from .core.text_report import (
    format_bytes,
    render_gc,
    render_overview,
    render_plugin_detail,
)
from .core.web_api import MemoryScopeWebApi

PLUGIN_ID = "astrbot_plugin_memory_scope"
PLUGIN_VERSION = "1.0.0"
# Key used with the plugin KV store so history survives a reload.
HISTORY_KEY = "history"
# Flush the ring buffer to the KV store every N samples instead of every sample.
PERSIST_EVERY = 5
PERSIST_KEEP = 240
# The very first sample is taken shortly after startup so the page is not empty.
FIRST_SAMPLE_DELAY_SECONDS = 8.0


@register(
    "MemoryScope",
    "Whereis-Alice",
    "按插件维度统计 AstrBot 的内存占用，并在 Dashboard 页面中查看归因、保留量、趋势与告警",
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
        self.collector = MemoryCollector(context, self.settings, PLUGIN_ID)
        self.web_api = MemoryScopeWebApi(PLUGIN_ID, self.collector)
        self._sampler_task: asyncio.Task[None] | None = None
        self._samples_since_persist = 0
        self._history_loaded = False

        if self.settings.auto_start_tracemalloc:
            # Starting here (rather than in initialize) captures as much of the
            # remaining plugin loading as possible.
            self.collector.probe.start()

        self._web_routes = self.web_api.register(context)
        if not self._web_routes:
            logger.warning(
                "MemoryScope 未能注册 Web API，Dashboard 页面将不可用"
                "（当前 AstrBot 版本可能过旧或缺少 quart）",
            )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        await self._load_history()
        if self._sampler_task is None or self._sampler_task.done():
            self._sampler_task = asyncio.create_task(self._sampler_loop())
        status = self.collector.probe.status()
        logger.info(
            "MemoryScope 已启动 · tracemalloc=%s(%s 帧) · 采样间隔 %ss · 深度扫描=%s",
            "on" if status["tracing"] else "off",
            status["frames"],
            self.settings.sample_interval_seconds,
            "on" if self.settings.deep_scan_enabled else "off",
        )
        if status["tracing"] and not status["covers_plugin_import"]:
            logger.info(
                "MemoryScope: tracemalloc 由本插件启动，插件导入期的内存不会被计入。"
                "如需完整数据，请以 PYTHONTRACEMALLOC=%s 启动 AstrBot。",
                self.settings.tracemalloc_frames,
            )

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
        # Only give back tracemalloc if we were the ones who turned it on, so a
        # reload never blinds a profiling session started by the operator.
        self.collector.probe.stop(only_if_started_by_plugin=True)
        logger.info("MemoryScope 已停止")

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        """Re-read the plugin catalog once every plugin finished loading."""
        self.collector.ensure_registry(force=True)

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
        while True:
            try:
                await self._sample_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.warning("MemoryScope 采样失败: %s", exc)
            await asyncio.sleep(max(10, self.settings.sample_interval_seconds))

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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("top")
    async def cmd_top(self, event: AstrMessageEvent, count: int = 0):
        """查看占用最高的插件。"""
        top_n = count if count and count > 0 else self.settings.command_top_n
        report = await self.collector.build_report(deep=False, record_sample=True)
        yield event.plain_result(render_overview(report, top_n=min(30, top_n)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("deep")
    async def cmd_deep(self, event: AstrMessageEvent, count: int = 0):
        """执行一次引用图深度扫描并查看结果。"""
        if not self.settings.deep_scan_enabled:
            yield event.plain_result("深度扫描已在配置中关闭（deep_scan_enabled）。")
            return
        yield event.plain_result("正在扫描引用图，可能需要几秒…")
        top_n = count if count and count > 0 else self.settings.command_top_n
        report = await self.collector.build_report(deep=True, record_sample=True)
        meta = report.get("deep_meta") or {}
        text = render_overview(report, top_n=min(30, top_n))
        suffix = f"\n\n深度扫描耗时 {meta.get('elapsed_ms') or 0} ms"
        if meta.get("truncated"):
            suffix += "（已达上限被截断，结果偏小）"
        yield event.plain_result(text + suffix)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mem.command("plugin")
    async def cmd_plugin(self, event: AstrMessageEvent, name: str = ""):
        """查看单个插件的分配明细。"""
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
    @mem.command("trace")
    async def cmd_trace(self, event: AstrMessageEvent, action: str = ""):
        """开关 tracemalloc：/mem trace on|off|peak。"""
        verb = (action or "").strip().lower()
        probe = self.collector.probe
        if verb in {"on", "start"}:
            probe.start()
        elif verb in {"off", "stop"}:
            probe.stop(only_if_started_by_plugin=False)
        elif verb in {"peak", "reset"}:
            probe.reset_peak()
        else:
            yield event.plain_result("用法：/mem trace on | off | peak")
            return
        status = probe.status()
        lines = [
            f"tracemalloc {'已开启' if status['tracing'] else '已关闭'}"
            f" · 深度 {status['frames']} 帧",
            f"当前 {format_bytes(status['current_bytes'])}"
            f" · 峰值 {format_bytes(status['peak_bytes'])}",
        ]
        if status["tracing"] and not status["covers_plugin_import"]:
            lines.append("⚠ 追踪在插件加载后启动，导入期内存未计入。")
        yield event.plain_result("\n".join(lines))

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