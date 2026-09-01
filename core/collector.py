"""Orchestrates the probes and turns them into the payload the UI consumes."""

from __future__ import annotations

import asyncio
import gc
import math
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import retained_scan
from .dep_audit import DEFAULT_MAX_FILES, DEFAULT_TIME_BUDGET_MS, DependencyAuditor
from .import_cost import DEFAULT_MAX_OVERHEAD_MS, RssReader, get_ledger
from .object_census import CensusResult, run_census
from .plugin_registry import PluginRegistry
from .retained_scan import ScanLimits
from .sampler import AlertEngine, HistoryStore, Sample

try:  # psutil ships with AstrBot, but never let a monitoring plugin hard-fail.
    import psutil  # type: ignore[import-not-found]

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


_PROC_STATUS_FIELDS = {
    "VmRSS": "rss_bytes",
    "VmSize": "vms_bytes",
    "RssAnon": "rss_anon_bytes",
    "RssFile": "rss_file_bytes",
    "RssShmem": "rss_shmem_bytes",
    "VmSwap": "swap_bytes",
}


def _read_proc_status() -> dict[str, int]:
    """Read cheap per-process memory counters on Linux.

    ``/proc/self/status`` is a small text file and does not walk the address
    space.  It gives small VPS users the split that a plain RSS number hides,
    especially how much of the process is currently swapped out.  Other
    platforms simply return an empty mapping.
    """

    if not os.path.exists("/proc/self/status"):
        return {}
    result: dict[str, int] = {}
    try:
        with open("/proc/self/status", "rb") as handle:
            for raw_line in handle:
                key, separator, raw_value = raw_line.partition(b":")
                target = _PROC_STATUS_FIELDS.get(key.decode("ascii", "ignore"))
                if target is None or not separator:
                    continue
                fields = raw_value.split()
                if not fields:
                    continue
                # The selected fields are reported in kB by procfs.
                result[target] = int(fields[0]) * 1024
    except (OSError, ValueError, TypeError):
        return {}
    return result


def _read_cgroup_memory() -> dict[str, int]:
    """Read cgroup v2/v1 memory counters when the host exposes them."""

    candidates = (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    )
    for current_path, limit_path in candidates:
        try:
            with open(current_path, "rb") as handle:
                current_raw = handle.read().strip()
            with open(limit_path, "rb") as handle:
                limit_raw = handle.read().strip()
            current = int(current_raw)
            # cgroup v2 uses the literal ``max`` for an unlimited group.
            if limit_raw == b"max":
                return {"cgroup_current_bytes": current}
            limit = int(limit_raw)
            # Some v1 hosts expose an effectively-unlimited sentinel.
            if limit > 0 and limit < (1 << 60):
                return {
                    "cgroup_current_bytes": current,
                    "cgroup_limit_bytes": limit,
                }
            return {"cgroup_current_bytes": current}
        except (OSError, ValueError, TypeError):
            continue
    return {}


@dataclass(slots=True)
class Settings:
    """Runtime knobs, mirrored from ``_conf_schema.json``."""

    measure_import_cost: bool = True
    import_hook_max_overhead_ms: float = DEFAULT_MAX_OVERHEAD_MS
    census_enabled: bool = False
    census_sample_rate: int = 10
    census_time_budget_ms: int = 4000
    dep_audit_enabled: bool = True
    dep_audit_max_files: int = DEFAULT_MAX_FILES
    dep_audit_time_budget_ms: int = DEFAULT_TIME_BUDGET_MS
    sample_interval_seconds: int = 60
    history_size: int = 720
    deep_scan_enabled: bool = True
    deep_scan_max_objects: int = 120_000
    deep_scan_max_objects_total: int = 400_000
    deep_scan_time_budget_ms: int = 3000
    include_object_count: bool = False
    alert_plugin_mb: float = 0.0
    alert_growth_mb_per_hour: float = 0.0
    alert_rss_mb: float = 0.0
    alert_rss_growth_mb_per_hour: float = 0.0
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
                raw = float(read(key, default))
                if not math.isfinite(raw):
                    return default
                return max(minimum, min(maximum, int(raw)))
            except (TypeError, ValueError, OverflowError):
                return default

        def as_float(key: str, default: float) -> float:
            try:
                value = float(read(key, default))
                if not math.isfinite(value):
                    return default
                return max(0.0, value)
            except (TypeError, ValueError, OverflowError):
                return default

        def as_bool(key: str, default: bool) -> bool:
            value = read(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"false", "0", "no", "off", "", "none", "null"}:
                    return False
                if normalized in {"true", "1", "yes", "on"}:
                    return True
            return bool(value)

        return cls(
            measure_import_cost=as_bool("measure_import_cost", True),
            import_hook_max_overhead_ms=as_float(
                "import_hook_max_overhead_ms", DEFAULT_MAX_OVERHEAD_MS,
            ),
            census_enabled=as_bool("census_enabled", False),
            census_sample_rate=as_int("census_sample_rate", 10, 1, 1000),
            census_time_budget_ms=as_int("census_time_budget_ms", 4000, 200, 60_000),
            dep_audit_enabled=as_bool("dep_audit_enabled", True),
            dep_audit_max_files=as_int(
                "dep_audit_max_files", DEFAULT_MAX_FILES, 10, 5000,
            ),
            dep_audit_time_budget_ms=as_int(
                "dep_audit_time_budget_ms", DEFAULT_TIME_BUDGET_MS, 200, 60_000,
            ),
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
            alert_rss_mb=as_float("alert_rss_mb", 0.0),
            alert_rss_growth_mb_per_hour=as_float("alert_rss_growth_mb_per_hour", 0.0),
            persist_history=as_bool("persist_history", True),
            command_top_n=as_int("command_top_n", 8, 1, 30),
            registry_ttl_seconds=as_float("registry_ttl_seconds", 30.0),
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
        self.ledger = get_ledger(settings.import_hook_max_overhead_ms)
        self.auditor = DependencyAuditor(
            max_files=settings.dep_audit_max_files,
            time_budget_ms=settings.dep_audit_time_budget_ms,
        )
        self.rss = RssReader()
        self.history = HistoryStore(settings.history_size)
        self.alerts = AlertEngine(
            settings.alert_plugin_mb,
            settings.alert_growth_mb_per_hour,
            settings.alert_rss_mb,
            settings.alert_rss_growth_mb_per_hour,
        )
        self._registry_stamp = 0.0
        self._lock = asyncio.Lock()
        self._process = psutil.Process(os.getpid()) if _PSUTIL_AVAILABLE else None
        self._census: CensusResult | None = None
        self._audit: dict[str, Any] | None = None
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
        self.ledger.max_overhead_ms = settings.import_hook_max_overhead_ms
        self.auditor.max_files = settings.dep_audit_max_files
        self.auditor.time_budget_ms = settings.dep_audit_time_budget_ms
        max_samples = max(30, settings.history_size)
        if self.history.max_samples != max_samples:
            old_samples = self.history.samples(max_samples)
            self.history.max_samples = max_samples
            self.history.replace_samples(old_samples)
        self.alerts.update_thresholds(
            settings.alert_plugin_mb,
            settings.alert_growth_mb_per_hour,
            settings.alert_rss_mb,
            settings.alert_rss_growth_mb_per_hour,
        )

    def ensure_registry(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._registry_stamp >= self.settings.registry_ttl_seconds:
            self.registry.refresh()
            self._registry_stamp = now

    def read_rss(self) -> int:
        """Cheap RSS read: the statm fd when available, psutil otherwise."""

        value = self.rss.read()
        if value:
            return value
        if self._process is not None:
            try:
                return int(self._process.memory_info().rss)
            except Exception:  # noqa: BLE001 - a probe must not raise
                return 0
        return 0

    def close(self) -> None:
        """Release the tiny procfs handle held by the RSS reader."""

        self.rss.close()

    def import_hook_status(self) -> dict[str, Any]:
        """Ledger status without the package table, for the process block."""

        status = self.ledger.snapshot(package_limit=1)
        status.pop("packages", None)
        status.pop("packages_truncated", None)
        status["enabled"] = self.settings.measure_import_cost
        return status

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
            "modules": len(sys.modules),
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
        if not stats["rss_bytes"]:
            stats["rss_bytes"] = self.read_rss()
        try:
            virtual = psutil.virtual_memory() if _PSUTIL_AVAILABLE else None
        except Exception:
            virtual = None
        if virtual is not None:
            stats["system_total_bytes"] = int(virtual.total)
            stats["system_available_bytes"] = int(virtual.available)

        # On Linux these counters are cheaper and more useful on a constrained
        # VPS than RSS alone: a process can have a large swapped-out portion
        # while its current RSS still looks deceptively comfortable.
        proc_stats = _read_proc_status()
        for key, value in proc_stats.items():
            if value >= 0:
                stats[key] = value
        stats.update(_read_cgroup_memory())
        cgroup_limit = stats.get("cgroup_limit_bytes")
        if isinstance(cgroup_limit, int) and cgroup_limit > 0:
            stats["cgroup_memory_percent"] = round(
                float(stats.get("cgroup_current_bytes") or 0)
                / cgroup_limit
                * 100.0,
                2,
            )

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
        stats["import_hook"] = self.import_hook_status()
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

    def _census_blocking(self) -> CensusResult:
        result = run_census(
            self.registry,
            sample_rate=self.settings.census_sample_rate,
            time_budget_ms=self.settings.census_time_budget_ms,
        )
        self._census = result
        return result

    def _audit_blocking(self) -> dict[str, Any]:
        result = self.auditor.run(self.registry.entries, self.ledger.cost_table())
        self._audit = result
        return result

    def _collect_blocking(
        self,
        deep: bool,
        census: bool,
        audit: bool,
    ) -> dict[str, Any]:
        if census:
            self._census_blocking()
        if audit:
            self._audit_blocking()
        deep_results = self._deep_scan() if deep else dict(self._last_deep)
        return {
            "census": self._census,
            "audit": self._audit,
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
        census: bool | None = None,
        audit: bool | None = None,
        record_sample: bool = True,
    ) -> dict[str, Any]:
        """Build the payload.

        ``census`` and ``audit`` default to the configured behaviour: the object
        census only runs when it is switched on, the dependency audit runs once
        and is then cached.  Passing ``True`` forces a fresh run, which is what
        the buttons and the chat commands do.
        """

        async with self._lock:
            self.ensure_registry()
            if deep and not self.settings.deep_scan_enabled:
                deep = False
            run_census_now = (
                self.settings.census_enabled if census is None else bool(census)
            )
            if audit is None:
                run_audit_now = self.settings.dep_audit_enabled and self._audit is None
            else:
                run_audit_now = bool(audit)
            raw = await asyncio.to_thread(
                self._collect_blocking,
                deep,
                run_census_now,
                run_audit_now,
            )
            process = self.process_stats(
                include_object_count=deep and self.settings.include_object_count,
            )
            # Keep the process-level baseline in the same payload as RSS.  A
            # baseline without a census is still useful for the one metric that
            # is always available on a small VPS: total resident memory.
            process["baseline_rss_bytes"] = (
                self.history.baseline.rss_bytes
                if self.history.baseline is not None
                else None
            )
            process["rss_delta_bytes"] = self.history.rss_delta(
                int(process.get("rss_bytes") or 0),
            )
            census_result: CensusResult | None = raw["census"]
            audit_result: dict[str, Any] | None = raw["audit"]
            rows = self._build_rows(census_result, audit_result, raw["deep"])
            if record_sample:
                # Recorded on every call: the process RSS trend is the one signal
                # that still works with every per-plugin probe switched off,
                # which is the default.
                # A cached census is useful for the current report but must not
                # be copied into every later RSS-only sample.  Otherwise a
                # manual one-off census would create a flat, fictitious trend.
                sample_census = census_result if run_census_now else None
                self._record_sample(process, sample_census, rows)
            hook = self.ledger.snapshot(package_limit=0)
            opportunities = list((audit_result or {}).get("opportunities") or [])
            payload: dict[str, Any] = {
                "generated_at": time.time(),
                "process": process,
                "plugins": rows,
                "packages": self._build_packages(hook),
                "census_buckets": (
                    census_result.bucket_rows() if census_result is not None else []
                ),
                "census_meta": (
                    census_result.meta() if census_result is not None else None
                ),
                "audit_meta": self._build_audit_meta(audit_result),
                "opportunities": opportunities[:40],
                "totals": {
                    "plugin_count": len(rows),
                    "measured_plugin_count": sum(
                        1 for row in rows if row["import_measured"]
                    ),
                    "import_self_bytes_total": int(hook.get("plugin_bytes") or 0),
                    "packages_bytes": int(hook.get("package_bytes") or 0),
                    "packages_count": int(hook.get("package_count") or 0),
                    "import_total_bytes": int(hook.get("plugin_bytes") or 0)
                    + int(hook.get("package_bytes") or 0),
                    "hook_rss_growth_bytes": int(hook.get("rss_growth_bytes") or 0),
                    "census_bytes": (
                        census_result.plugin_bytes if census_result is not None else 0
                    ),
                    "census_objects": (
                        census_result.plugin_objects if census_result is not None else 0
                    ),
                    "lazy_savings_bytes": sum(
                        int(row.get("cost_bytes") or 0) for row in opportunities
                    ),
                    "rss_bytes": int(process.get("rss_bytes") or 0),
                },
                "deep_meta": raw["deep_meta"],
                "history": {
                    "samples": self.history.count(),
                    "census_samples": len(self.history.census_samples()),
                    "interval_seconds": self.settings.sample_interval_seconds,
                    "baseline_at": (
                        self.history.baseline.ts if self.history.baseline else None
                    ),
                },
                "notes": self._build_notes(census_result, audit_result, rows),
                "self_plugin": self.self_plugin_name,
            }
            if detail_for is not None:
                payload["detail"] = self._build_detail(
                    detail_for, audit_result, census_result, rows,
                )
            return payload

    def _record_sample(
        self,
        process: dict[str, Any],
        census: CensusResult | None,
        rows: list[dict[str, Any]],
    ) -> list[Any]:
        rss_bytes = int(process.get("rss_bytes") or 0)
        sample = Sample(
            ts=time.time(),
            rss_bytes=rss_bytes,
            census_bytes=census.plugin_bytes if census is not None else 0,
            plugins=census.plugin_map() if census is not None else {},
            census_ran=census is not None,
        )
        self.history.add(sample)
        fired: list[Any] = []
        # RSS rules work with every probe off, so they are always evaluated.
        fired.extend(
            self.alerts.evaluate_process(
                rss_bytes,
                self.history.rss_trend_bytes_per_minute(),
                now=sample.ts,
            ),
        )
        if sample.has_attribution:
            # Without a census every row reads 0 bytes; evaluating per-plugin
            # thresholds on that would only produce noise.
            fired.extend(self.alerts.evaluate(rows, now=sample.ts))
        self.last_alerts = fired
        return fired


    def _build_rows(
        self,
        census: CensusResult | None,
        audit_result: dict[str, Any] | None,
        deep_results: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        audits = (audit_result or {}).get("audits") or {}
        census_plugins = census.plugins if census is not None else {}
        rows: list[dict[str, Any]] = []
        for entry in self.registry.entries:
            # Import costs are keyed by directory name, registry rows by the
            # ``@register`` name; they differ for most plugins.
            cost = self.ledger.plugin_cost(entry.import_key)
            measured = cost is not None
            audit = audits.get(entry.name)
            seen = census_plugins.get(entry.name)
            census_bytes = seen.bytes if seen is not None else 0
            row: dict[str, Any] = {
                **entry.to_dict(),
                "is_self": (
                    entry.name == self.self_plugin_name
                    or entry.import_key == self.self_plugin_name
                ),
                # ``None`` rather than 0 whenever the plugin was imported before
                # this plugin installed its hook: unknown is not the same as free.
                "import_measured": measured,
                "import_bytes": cost.bytes if cost is not None else None,
                "import_self_bytes": cost.self_bytes if cost is not None else None,
                "import_ms": round(cost.wall_ms, 1) if cost is not None else None,
                "import_modules": cost.modules if cost is not None else None,
                "import_packages": list(cost.packages) if cost is not None else [],
                "census_bytes": census_bytes,
                "census_objects": seen.objects if seen is not None else 0,
                "census_measured": census is not None,
                "census_types": seen.top_types(6) if seen is not None else [],
                "lazy_savings_bytes": audit.known_bytes if audit is not None else 0,
                "audit_findings": len(audit.imports) if audit is not None else 0,
                "audit_error": audit.error if audit is not None else None,
                "delta_bytes": (
                    self.history.delta(entry.name, census_bytes)
                    if census is not None
                    else None
                ),
                "trend_bytes_per_minute": self.history.trend_bytes_per_minute(
                    entry.name,
                ),
                "retained": deep_results.get(entry.name),
            }
            rows.append(row)
        rows.sort(
            key=lambda item: (item["import_bytes"] or 0, item["census_bytes"]),
            reverse=True,
        )
        return rows

    def _build_packages(
        self,
        hook: dict[str, Any],
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        packages = list(hook.get("packages") or [])
        head = packages[:limit]
        rest = packages[limit:]
        rows = list(head)
        if rest:
            rows.append(
                {
                    "name": "…",
                    "bytes": sum(int(item.get("bytes") or 0) for item in rest),
                    "self_bytes": sum(int(item.get("self_bytes") or 0) for item in rest),
                    "wall_ms": round(
                        sum(float(item.get("wall_ms") or 0.0) for item in rest), 1,
                    ),
                    "modules": sum(int(item.get("modules") or 0) for item in rest),
                    "imports": sum(int(item.get("imports") or 0) for item in rest),
                    "first_importer": None,
                    "aggregated": len(rest),
                },
            )
        return rows

    @staticmethod
    def _build_audit_meta(audit_result: dict[str, Any] | None) -> dict[str, Any] | None:
        if not audit_result:
            return None
        audits = audit_result.get("audits") or {}
        unknown = sorted(
            {module for audit in audits.values() for module in audit.unknown_modules},
        )
        return {
            "generated_at": audit_result.get("generated_at"),
            "elapsed_ms": audit_result.get("elapsed_ms"),
            "time_budget_hit": bool(audit_result.get("time_budget_hit")),
            "pending": int(audit_result.get("pending") or 0),
            "cost_table_size": int(audit_result.get("cost_table_size") or 0),
            "plugin_count": len(audits),
            "audited": sum(1 for audit in audits.values() if audit.error is None),
            "finding_count": sum(len(audit.imports) for audit in audits.values()),
            "unknown_modules": unknown[:40],
            "unknown_total": len(unknown),
        }

    def _build_detail(
        self,
        plugin: str,
        audit_result: dict[str, Any] | None,
        census: CensusResult | None,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry = self.registry.get(plugin)
        row = next((item for item in rows if item["name"] == plugin), None)
        import_key = entry.import_key if entry is not None else plugin
        cost = self.ledger.plugin_cost(import_key)
        audit = ((audit_result or {}).get("audits") or {}).get(plugin)
        seen = census.plugins.get(plugin) if census is not None else None
        packages: list[dict[str, Any]] = []
        if cost is not None and cost.packages:
            table = {item.name: item for item in self.ledger.packages_sorted()}
            packages = [
                table[name].to_dict() for name in cost.packages if name in table
            ]
            packages.sort(key=lambda item: item["bytes"], reverse=True)
        return {
            "name": plugin,
            "found": entry is not None,
            "row": row,
            "submodules": [
                getattr(module, "__name__", "?")
                for module in (entry.submodules if entry else [])
            ],
            "import": cost.to_dict() if cost is not None else None,
            "import_packages": packages,
            "audit": audit.to_dict() if audit is not None else None,
            "census": seen.to_dict(top_types=12) if seen is not None else None,
            "series": self.history.series(plugin, limit=240),
        }

    def _build_notes(
        self,
        census: CensusResult | None,
        audit_result: dict[str, Any] | None,
        rows: list[dict[str, Any]],
    ) -> list[str]:
        notes: list[str] = []
        if (
            not self.ledger.installed
            and not self.ledger.plugins
            and self.ledger.installed_at is None
        ):
            notes.append("import_hook_not_installed")
        if self.ledger.degraded:
            notes.append("import_hook_degraded")
        if rows and any(not row["import_measured"] for row in rows):
            notes.append("partial_import_coverage")
        if census is None:
            notes.append("census_never_run")
        else:
            if census.scaled:
                notes.append("census_sampled")
            if census.truncated:
                notes.append("census_truncated")
        if audit_result is None:
            notes.append("dep_audit_never_run")
        elif audit_result.get("time_budget_hit"):
            notes.append("dep_audit_truncated")
        if not _PSUTIL_AVAILABLE:
            notes.append("psutil_missing")
        if not self.rss.available:
            notes.append("rss_reader_unavailable")
        if self._last_deep_truncated:
            notes.append("deep_scan_truncated")
        return notes

    # ------------------------------------------------------------------
    async def sample_once(self) -> list[Any]:
        """Take one history sample and return the alerts it triggered."""

        self.last_alerts = []
        await self.build_report(deep=False, record_sample=True)
        return list(self.last_alerts)

    async def census_now(self) -> dict[str, Any]:
        """Force one object census.  Expensive; only ever user-triggered."""

        return await self.build_report(census=True)

    async def audit_now(self) -> dict[str, Any]:
        """Force one dependency audit (source scan, no runtime cost)."""

        # An AST scan does not observe a new memory sample.  Keeping it out of
        # the history prevents clicking the audit button from looking like an
        # extra RSS tick in the trend chart.
        return await self.build_report(audit=True, record_sample=False)

    async def force_gc(self) -> dict[str, Any]:
        before = self.read_rss()
        collected = await asyncio.to_thread(gc.collect)
        after = self.read_rss()
        return {
            "collected": int(collected),
            "rss_before": before,
            "rss_after": after,
            # RSS rarely drops after a collect: CPython returns arenas to the
            # allocator, not to the kernel.  A zero here is normal, not a bug.
            "freed_bytes": max(0, before - after),
            "uncollectable": len(gc.garbage),
        }
