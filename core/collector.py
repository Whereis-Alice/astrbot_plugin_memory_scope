"""Orchestrates the probes and turns them into the payload the UI consumes."""

from __future__ import annotations

import asyncio
import gc
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import retained_scan
from .plugin_registry import PluginRegistry
from .retained_scan import ScanLimits
from .sampler import AlertEngine, HistoryStore, Sample
from .tracemalloc_probe import DEFAULT_FRAMES, TracemallocProbe

try:  # psutil ships with AstrBot, but never let a monitoring plugin hard-fail.
    import psutil  # type: ignore[import-not-found]

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


def available_memory_mb() -> float | None:
    """Free-plus-reclaimable memory in MiB, or ``None`` without psutil."""

    if not _PSUTIL_AVAILABLE:
        return None
    try:
        return float(psutil.virtual_memory().available) / (1024 * 1024)
    except Exception:  # noqa: BLE001 - a monitor must not raise on its own probe
        return None


def autostart_memory_block_reason(
    floor_mb: float,
    available_mb: float | None,
) -> str | None:
    """Explain why auto-starting tracemalloc is unsafe, or ``None`` when it is.

    tracemalloc keeps a traceback per live allocation, so switching it on costs
    memory on exactly the host that is already short of it.  ``floor_mb <= 0``
    disables the guard, and a missing reading never blocks.
    """

    if floor_mb <= 0 or available_mb is None:
        return None
    if available_mb < floor_mb:
        return f"系统可用内存仅 {available_mb:.0f} MB，低于安全阈值 {floor_mb:.0f} MB"
    return None


@dataclass(slots=True)
class Settings:
    """Runtime knobs, mirrored from ``_conf_schema.json``."""

    auto_start_tracemalloc: bool = False
    auto_start_min_available_mb: float = 512.0
    tracemalloc_frames: int = DEFAULT_FRAMES
    sample_interval_seconds: int = 60
    history_size: int = 720
    deep_scan_enabled: bool = True
    deep_scan_max_objects: int = 120_000
    deep_scan_max_objects_total: int = 400_000
    deep_scan_time_budget_ms: int = 3000
    include_object_count: bool = False
    alert_plugin_mb: float = 0.0
    alert_growth_mb_per_hour: float = 0.0
    persist_history: bool = True
    command_top_n: int = 8
    registry_ttl_seconds: float = 30.0

    @classmethod
    def from_config(cls, config: Any) -> "Settings":
        def read(key: str, default: Any) -> Any:
            try:
                value = config.get(key, default)  # type: ignore[union-attr]
            except (AttributeError, TypeError):
                return default
            return default if value is None else value

        def as_int(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(maximum, int(read(key, default))))
            except (TypeError, ValueError):
                return default

        def as_float(key: str, default: float) -> float:
            try:
                return max(0.0, float(read(key, default)))
            except (TypeError, ValueError):
                return default

        def as_bool(key: str, default: bool) -> bool:
            return bool(read(key, default))

        return cls(
            auto_start_tracemalloc=as_bool("auto_start_tracemalloc", False),
            auto_start_min_available_mb=as_float("auto_start_min_available_mb", 512.0),
            tracemalloc_frames=as_int("tracemalloc_frames", DEFAULT_FRAMES, 1, 40),
            sample_interval_seconds=as_int("sample_interval_seconds", 60, 10, 3600),
            history_size=as_int("history_size", 720, 30, 5000),
            deep_scan_enabled=as_bool("deep_scan_enabled", True),
            deep_scan_max_objects=as_int("deep_scan_max_objects", 120_000, 5_000, 2_000_000),
            deep_scan_max_objects_total=as_int(
                "deep_scan_max_objects_total", 400_000, 10_000, 8_000_000,
            ),
            deep_scan_time_budget_ms=as_int("deep_scan_time_budget_ms", 3000, 200, 60_000),
            include_object_count=as_bool("include_object_count", False),
            alert_plugin_mb=as_float("alert_plugin_mb", 0.0),
            alert_growth_mb_per_hour=as_float("alert_growth_mb_per_hour", 0.0),
            persist_history=as_bool("persist_history", True),
            command_top_n=as_int("command_top_n", 8, 1, 30),
        )


class MemoryCollector:
    """Single entry point used by both the Web API and the chat commands."""

    def __init__(
        self,
        context: Any,
        settings: Settings,
        self_plugin_name: str,
    ) -> None:
        self.context = context
        self.settings = settings
        self.self_plugin_name = self_plugin_name
        self.registry = PluginRegistry(context)
        self.probe = TracemallocProbe(settings.tracemalloc_frames)
        self.history = HistoryStore(settings.history_size)
        self.alerts = AlertEngine(
            settings.alert_plugin_mb,
            settings.alert_growth_mb_per_hour,
        )
        self._registry_stamp = 0.0
        self._lock = asyncio.Lock()
        self._process = psutil.Process(os.getpid()) if _PSUTIL_AVAILABLE else None
        self._last_deep: dict[str, dict[str, Any]] = {}
        self._last_deep_ts = 0.0
        self._last_deep_elapsed = 0.0
        self._last_deep_truncated = False
        # Alerts fired by the most recent recorded sample, consumed by main.py.
        self.last_alerts: list[Any] = []
        self.registry.refresh()
        self._registry_stamp = time.monotonic()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.probe.frames = settings.tracemalloc_frames
        self.history.max_samples = max(30, settings.history_size)
        self.alerts.update_thresholds(
            settings.alert_plugin_mb,
            settings.alert_growth_mb_per_hour,
        )

    def ensure_registry(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._registry_stamp >= self.settings.registry_ttl_seconds:
            self.registry.refresh()
            self._registry_stamp = now

    def process_stats(self, include_object_count: bool = False) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "pid": os.getpid(),
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "threads": threading.active_count(),
            "psutil_available": _PSUTIL_AVAILABLE,
            "rss_bytes": 0,
            "vms_bytes": 0,
            "memory_percent": None,
            "uptime_seconds": None,
        }
        if self._process is not None:
            try:
                info = self._process.memory_info()
                stats["rss_bytes"] = int(getattr(info, "rss", 0) or 0)
                stats["vms_bytes"] = int(getattr(info, "vms", 0) or 0)
                stats["memory_percent"] = round(
                    float(self._process.memory_percent()), 3,
                )
                stats["uptime_seconds"] = round(
                    max(0.0, time.time() - self._process.create_time()), 1,
                )
            except Exception:
                pass
        try:
            virtual = psutil.virtual_memory() if _PSUTIL_AVAILABLE else None
        except Exception:
            virtual = None
        if virtual is not None:
            stats["system_total_bytes"] = int(virtual.total)
            stats["system_available_bytes"] = int(virtual.available)

        counts = gc.get_count()
        stats["gc"] = {
            "counts": list(counts),
            "thresholds": list(gc.get_threshold()),
            "collections": [
                int(entry.get("collections", 0)) for entry in gc.get_stats()
            ],
            "uncollectable": len(gc.garbage),
        }
        if include_object_count:
            try:
                stats["gc"]["tracked_objects"] = len(gc.get_objects())
            except Exception:
                stats["gc"]["tracked_objects"] = None
        stats["tracemalloc"] = self.probe.status()
        return stats

    # ------------------------------------------------------------------
    # blocking work (executed in a worker thread)
    # ------------------------------------------------------------------
    def _deep_scan(self) -> dict[str, dict[str, Any]]:
        entries = [
            entry
            for entry in self.registry.entries
            if entry.has_instance or entry.module is not None
        ]
        if not entries:
            self._last_deep = {}
            return {}

        star_objects = [entry.star_cls for entry in entries if entry.star_cls is not None]
        denylist = retained_scan.build_denylist(self.context, extra_roots=star_objects)
        # The plugin instances themselves are reachable from the core registry,
        # yet they are exactly what has to be measured, so un-deny them.
        for entry in entries:
            for candidate in (entry.star_cls, getattr(entry.star_cls, "__dict__", None)):
                if candidate is not None:
                    denylist.discard(id(candidate))

        roots_by_plugin: dict[str, list[Any]] = {}
        for entry in entries:
            roots: list[Any] = []
            if entry.star_cls is not None:
                roots.append(entry.star_cls)
            module_dict = getattr(entry.module, "__dict__", None)
            if isinstance(module_dict, dict):
                roots.append(module_dict)
            for submodule in entry.submodules:
                sub_dict = getattr(submodule, "__dict__", None)
                if isinstance(sub_dict, dict):
                    roots.append(sub_dict)
            if roots:
                roots_by_plugin[entry.name] = roots

        report = retained_scan.scan(
            roots_by_plugin,
            denylist,
            ScanLimits(
                max_objects_per_plugin=self.settings.deep_scan_max_objects,
                max_objects_total=self.settings.deep_scan_max_objects_total,
                time_budget_ms=self.settings.deep_scan_time_budget_ms,
            ),
        )
        self._last_deep = {
            name: result.to_dict() for name, result in report.results.items()
        }
        self._last_deep_ts = time.time()
        self._last_deep_elapsed = round(report.elapsed_ms, 1)
        self._last_deep_truncated = report.truncated
        return self._last_deep

    def _collect_blocking(
        self,
        deep: bool,
        detail_for: str | None,
        line_limit: int,
    ) -> dict[str, Any]:
        snapshot = self.probe.snapshot()
        attribution = None
        if snapshot is not None:
            attribution = self.probe.analyze(
                snapshot,
                self.registry,
                line_detail_for=detail_for,
                line_limit=line_limit,
            )
            del snapshot
        deep_results = self._deep_scan() if deep else dict(self._last_deep)
        return {
            "attribution": attribution,
            "deep": deep_results,
            "deep_meta": {
                "generated_at": self._last_deep_ts or None,
                "elapsed_ms": self._last_deep_elapsed or None,
                "truncated": self._last_deep_truncated,
                "fresh": bool(deep),
            },
        }

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def build_report(
        self,
        deep: bool = False,
        detail_for: str | None = None,
        line_limit: int = 25,
        record_sample: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            self.ensure_registry()
            if deep and not self.settings.deep_scan_enabled:
                deep = False
            raw = await asyncio.to_thread(
                self._collect_blocking,
                deep,
                detail_for,
                line_limit,
            )
            process = self.process_stats(
                include_object_count=deep and self.settings.include_object_count,
            )
            attribution = raw["attribution"]
            rows = self._build_rows(attribution, raw["deep"])
            if record_sample:
                # Recorded even without a tracemalloc snapshot: the process RSS
                # trend is the one signal that still works with tracing off,
                # which is now the default.
                self._record_sample(process, attribution, rows)
            payload: dict[str, Any] = {
                "generated_at": time.time(),
                "process": process,
                "plugins": rows,
                "others": self._build_others(attribution),
                "totals": {
                    "traced_bytes": getattr(attribution, "total_bytes", 0),
                    "traced_blocks": getattr(attribution, "traced_blocks", 0),
                    "plugin_bytes": sum(int(r["attributed_bytes"]) for r in rows),
                    "plugin_count": len(rows),
                    "measured_plugin_count": sum(
                        1 for r in rows if r["attributed_bytes"] > 0
                    ),
                },
                "deep_meta": raw["deep_meta"],
                "history": {
                    "samples": self.history.count(),
                    "traced_samples": len(self.history.traced_samples()),
                    "interval_seconds": self.settings.sample_interval_seconds,
                    "baseline_at": (
                        self.history.baseline.ts if self.history.baseline else None
                    ),
                },
                "notes": self._build_notes(attribution),
                "self_plugin": self.self_plugin_name,
            }
            if detail_for is not None:
                payload["detail"] = self._build_detail(detail_for, attribution, rows)
            return payload

    def _record_sample(
        self,
        process: dict[str, Any],
        attribution: Any,
        rows: list[dict[str, Any]],
    ) -> list[Any]:
        sample = Sample(
            ts=time.time(),
            rss_bytes=int(process.get("rss_bytes") or 0),
            traced_bytes=int(getattr(attribution, "total_bytes", 0)),
            plugins={
                str(row["name"]): int(row["attributed_bytes"])
                for row in rows
                if row["attributed_bytes"] > 0
            },
        )
        self.history.add(sample)
        if attribution is None:
            # Every row reads 0 bytes without a snapshot; evaluating thresholds
            # on that would clear real alerts and hide real growth.
            self.last_alerts = []
            return self.last_alerts
        self.last_alerts = self.alerts.evaluate(rows, now=sample.ts)
        return self.last_alerts

    def _build_rows(
        self,
        attribution: Any,
        deep_results: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        alloc_map = getattr(attribution, "plugins", {}) or {}
        total = max(1, int(getattr(attribution, "total_bytes", 0) or 0))
        rows: list[dict[str, Any]] = []
        for entry in self.registry.entries:
            alloc = alloc_map.get(entry.name)
            attributed = int(getattr(alloc, "attributed_bytes", 0) or 0)
            row: dict[str, Any] = {
                **entry.to_dict(),
                "is_self": entry.name == self.self_plugin_name,
                "attributed_bytes": attributed,
                "direct_bytes": int(getattr(alloc, "direct_bytes", 0) or 0),
                "blocks": int(getattr(alloc, "blocks", 0) or 0),
                "traced_share": round(attributed * 100.0 / total, 3),
                "delta_bytes": self.history.delta(entry.name, attributed),
                "trend_bytes_per_minute": self.history.trend_bytes_per_minute(
                    entry.name,
                ),
                "retained": deep_results.get(entry.name),
            }
            rows.append(row)
        rows.sort(key=lambda item: item["attributed_bytes"], reverse=True)
        return rows

    def _build_others(self, attribution: Any, limit: int = 14) -> list[dict[str, Any]]:
        others = dict(getattr(attribution, "others", {}) or {})
        ordered = sorted(others.items(), key=lambda item: item[1], reverse=True)
        head = ordered[:limit]
        rest = ordered[limit:]
        rows = [{"bucket": name, "bytes": int(size)} for name, size in head]
        if rest:
            rows.append(
                {
                    "bucket": "…",
                    "bytes": int(sum(size for _name, size in rest)),
                    "aggregated": len(rest),
                },
            )
        return rows

    def _build_detail(
        self,
        plugin: str,
        attribution: Any,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry = self.registry.get(plugin)
        row = next((item for item in rows if item["name"] == plugin), None)
        lines = getattr(attribution, "lines", []) or []
        return {
            "name": plugin,
            "found": entry is not None,
            "row": row,
            "submodules": [
                getattr(module, "__name__", "?") for module in (entry.submodules if entry else [])
            ],
            "lines": [
                {
                    "filename": line.filename,
                    "lineno": line.lineno,
                    "bytes": line.size,
                    "blocks": line.blocks,
                }
                for line in lines
            ],
            "series": self.history.series(plugin, limit=240),
        }

    def _build_notes(self, attribution: Any) -> list[str]:
        notes: list[str] = []
        status = self.probe.status()
        if not status["tracing"]:
            notes.append("tracemalloc_off")
        elif not status["covers_plugin_import"]:
            notes.append("tracing_started_late")
        if attribution is None:
            notes.append("no_snapshot")
        if not _PSUTIL_AVAILABLE:
            notes.append("psutil_missing")
        if self._last_deep_truncated:
            notes.append("deep_scan_truncated")
        return notes

    # ------------------------------------------------------------------
    async def sample_once(self) -> list[Any]:
        """Take one history sample and return the alerts it triggered."""

        self.last_alerts = []
        await self.build_report(deep=False, record_sample=True)
        return list(self.last_alerts)

    async def force_gc(self) -> dict[str, Any]:
        before = self.probe.traced_memory()[0]
        collected = await asyncio.to_thread(gc.collect)
        after = self.probe.traced_memory()[0]
        return {
            "collected": int(collected),
            "traced_before": before,
            "traced_after": after,
            "freed_bytes": max(0, before - after),
            "uncollectable": len(gc.garbage),
        }