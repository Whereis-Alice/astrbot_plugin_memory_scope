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

from .core.collector import (
    MemoryCollector,
    Settings,
    autostart_memory_block_reason,
    available_memory_mb,
)
from .core.sampler import wait_until_loaded
from .core.text_report import (
    format_bytes,
    render_gc,
    render_overview,
    render_plugin_detail,
)
from .core.web_api import MemoryScopeWebApi

PLUGIN_ID = "astrbot_plugin_memory_scope"
PLUGIN_VERSION = "1.0.3"
# Key used with the plugin KV store so history survives a reload.
HISTORY_KEY = "history"
# Flush the ring buffer to the KV store every N samples instead of every sample.
PERSIST_EVERY = 5
PERSIST_KEEP = 240
# The first sample is taken shortly after AstrBot finished loading *every*
# plugin, never during the import phase: a tracemalloc snapshot competes with
# the imports for both CPU and RAM.
FIRST_SAMPLE_DELAY_SECONDS = 8.0
# on_astrbot_loaded is dispatched once per process, from CoreLifecycle.start().
# A plugin installed, enabled or reloaded from the Dashboard never receives it,
# so the sampler waits at most this long before assuming the import phase is
# long over and it is safe to start sampling.
LOADED_WAIT_TIMEOUT_SECONDS = 180.0
# When one sample costs more than this share of the interval, the loop backs off.
SAMPLE_COST_RATIO = 0.2
MAX_INTERVAL_MULTIPLIER = 8


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
        self._interval_multiplier = 1
        # Set by the on_astrbot_loaded hook.  tracemalloc is deliberately NOT
        # started here: starting it mid-import makes every plugin loaded after
        # MemoryScope allocate through the trace hook, which measured 39s -> 21min
        # of plugin loading on a 2-core / 1.6 GB host (see README).
        self._loaded_event = asyncio.Event()

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
        if status["tracing"]:
            tracing_state = "on"
        elif self.settings.auto_start_tracemalloc:
            tracing_state = "pending(等 AstrBot 加载完成)"
        else:
            tracing_state = "off"
        logger.info(
            "MemoryScope 已启动 · tracemalloc=%s(%s 帧) · 采样间隔 %ss · 深度扫描=%s",
            tracing_state,
            status["frames"],
            self.settings.sample_interval_seconds,
            "on" if self.settings.deep_scan_enabled else "off",
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
        """Re-read the plugin catalog and only *now* touch tracemalloc."""
        self.collector.ensure_registry(force=True)
        self._start_tracing_if_configured()
        self._loaded_event.set()

    def _auto_start_blocked_reason(self) -> str | None:
        """Why auto-start must be skipped, or ``None`` when it is safe.

        tracemalloc keeps one traceback per live allocation, so on a host that is
        already short on RAM turning it on trades a diagnosis for a swap storm.
        The guard makes MemoryScope refuse to be the cause of the problem it is
        supposed to measure.
        """

        return autostart_memory_block_reason(
            self.settings.auto_start_min_available_mb,
            available_memory_mb(),
        )

    def _start_tracing_if_configured(self) -> None:
        if not self.settings.auto_start_tracemalloc:
            return
        probe = self.collector.probe
        if probe.is_tracing():
            return
        blocked = self._auto_start_blocked_reason()
        if blocked is not None:
            logger.warning(
                "MemoryScope 未自动开启 tracemalloc：%s。"
                "需要归因时用 /mem trace on 或页面按钮手动开启，"
                "或调低 auto_start_min_available_mb。",
                blocked,
            )
            return
        probe.start()
        logger.info(
            "MemoryScope 已开启 tracemalloc(%s 帧)，只统计此刻之后的分配。"
            "需要包含插件导入期的数据请以 PYTHONTRACEMALLOC=%s 启动 AstrBot。",
            probe.effective_frames,
            self.settings.tracemalloc_frames,
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
        # Wait for every plugin to finish loading, otherwise the first snapshot
        # lands in the middle of the import storm it is meant to observe.  The
        # wait is bounded because the hook that sets the event only fires during
        # AstrBot startup: a runtime install would wait for it forever.
        loaded = await wait_until_loaded(
            self._loaded_event,
            LOADED_WAIT_TIMEOUT_SECONDS,
        )
        if not loaded:
            logger.info(
                "MemoryScope 等待 on_astrbot_loaded 超时(%.0fs)，"
                "按运行时安装/重载处理，开始采样。",
                LOADED_WAIT_TIMEOUT_SECONDS,
            )
            if self.settings.auto_start_tracemalloc:
                logger.warning(
                    "MemoryScope 本次不会自动开启 tracemalloc："
                    "自动开启只在 AstrBot 启动完成后的钩子里进行，"
                    "以免在插件导入期拖慢启动。需要归因请执行 /mem trace on "
                    "或用页面按钮开启，也可以重启 AstrBot。",
                )
            self.collector.ensure_registry(force=True)
        await asyncio.sleep(FIRST_SAMPLE_DELAY_SECONDS)
        while True:
            interval = max(10, self.settings.sample_interval_seconds)
            try:
                started = time.monotonic()
                await self._sample_tick()
                self._adjust_interval(time.monotonic() - started, interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.warning("MemoryScope 采样失败: %s", exc)
            await asyncio.sleep(interval * self._interval_multiplier)

    def _adjust_interval(self, elapsed: float, interval: int) -> None:
        """Back off when snapshots get expensive on a big or swapping process."""

        budget = interval * SAMPLE_COST_RATIO
        if elapsed > budget and self._interval_multiplier < MAX_INTERVAL_MULTIPLIER:
            self._interval_multiplier = min(
                MAX_INTERVAL_MULTIPLIER,
                self._interval_multiplier * 2,
            )
            logger.warning(
                "MemoryScope 单次采样耗时 %.1fs（超过间隔的 %.0f%%），"
                "采样间隔临时放大为 %sx（%ss）。",
                elapsed,
                SAMPLE_COST_RATIO * 100,
                self._interval_multiplier,
                interval * self._interval_multiplier,
            )
        elif self._interval_multiplier > 1 and elapsed < budget / 2:
            self._interval_multiplier = max(1, self._interval_multiplier // 2)

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