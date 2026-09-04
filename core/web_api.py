"""Dashboard Web API backing pages/memory/index.html.

AstrBot already gates ``/api/plug/...`` behind the dashboard session, so these
routes inherit the dashboard authentication and add no auth of their own.  Every
handler is read-only except the explicit census/audit/baseline/GC actions.
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from .collector import MemoryCollector

try:  # AstrBot 4.27+ exposes a framework-neutral web helper.
    from astrbot.api.web import (  # type: ignore[attr-defined]
        error_response,
        json_response,
        request,
    )

    _WEB_AVAILABLE = True
except ImportError:  # pragma: no cover - AstrBot <= 4.26 runs on quart
    try:
        from quart import jsonify, request  # type: ignore[import-not-found]

        _WEB_AVAILABLE = True
    except ImportError:
        _WEB_AVAILABLE = False
        jsonify = None  # type: ignore[assignment]
        request = None  # type: ignore[assignment]

    def json_response(  # type: ignore[misc]
        data: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = jsonify({} if data is None else data)
        response.status_code = status_code
        if headers:
            response.headers.update(headers)
        return response

    def error_response(  # type: ignore[misc]
        message: str,
        *,
        status_code: int = 400,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return json_response(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers,
        )


def ok(data: Any) -> Any:
    return json_response({"status": "ok", "data": data})


async def _query() -> dict[str, str]:
    if request is None:
        return {}
    # AstrBot's current neutral request exposes ``query``; older Quart
    # compatibility contexts expose ``args``.  Support both without importing
    # either framework into the rest of the plugin.
    args = getattr(request, "query", None)
    if args is None:
        args = getattr(request, "args", None)
    if args is None:
        return {}
    try:
        return {key: str(args.get(key)) for key in args.keys()}
    except Exception:
        return {}


async def _body() -> dict[str, Any]:
    if request is None:
        return {}
    neutral_json = getattr(request, "json", None)
    if callable(neutral_json):
        try:
            result = neutral_json(default={})
        except TypeError:
            result = neutral_json()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return result
    getter = getattr(request, "get_json", None)
    if callable(getter):
        try:
            result = getter(force=True, silent=True)
        except TypeError:
            result = getter()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return result
    raw_json = getattr(request, "json", None)
    if inspect.isawaitable(raw_json):
        raw_json = await raw_json
    return raw_json if isinstance(raw_json, dict) else {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_opt_bool(value: Any) -> bool | None:
    """``None`` when the parameter is absent, so the caller keeps its default."""

    if value is None or str(value).strip() == "":
        return None
    return _as_bool(value, False)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class MemoryScopeWebApi:
    """Registers and serves the plugin page endpoints."""

    def __init__(self, plugin_name: str, collector: MemoryCollector) -> None:
        self.plugin_name = plugin_name
        self.collector = collector
        self.routes: list[str] = []

    @property
    def available(self) -> bool:
        return _WEB_AVAILABLE

    def register(self, context: Any) -> list[str]:
        register = getattr(context, "register_web_api", None)
        if not _WEB_AVAILABLE or not callable(register):
            return []
        endpoints: list[tuple[str, Callable[..., Any], list[str], str]] = [
            ("overview", self.get_overview, ["GET"], "MemoryScope 进程概览"),
            ("plugins", self.get_plugins, ["GET"], "MemoryScope 各插件内存占用"),
            ("detail", self.get_detail, ["GET"], "MemoryScope 单插件分配明细"),
            ("history", self.get_history, ["GET"], "MemoryScope 采样历史"),
            ("alerts", self.get_alerts, ["GET"], "MemoryScope 告警记录"),
            ("imports", self.get_imports, ["GET"], "MemoryScope 加载成本与依赖审计"),
            ("census", self.post_census, ["POST"], "MemoryScope 手动对象普查"),
            ("audit", self.post_audit, ["POST"], "MemoryScope 手动依赖审计"),
            ("baseline", self.post_baseline, ["POST"], "MemoryScope 基线管理"),
            ("gc", self.post_gc, ["POST"], "MemoryScope 手动触发 GC"),
        ]
        registered: list[str] = []
        for suffix, handler, methods, desc in endpoints:
            route = f"/{self.plugin_name}/{suffix}"
            register(route, handler, methods, desc)
            registered.append(f"{'/'.join(methods)} /api/plug{route}")
        self.routes = registered
        return registered

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------
    async def get_overview(self) -> Any:
        settings = self.collector.settings
        return ok(
            {
                "process": self.collector.process_stats(),
                "settings": {
                    "sample_interval_seconds": settings.sample_interval_seconds,
                    "measure_import_cost": settings.measure_import_cost,
                    "census_enabled": settings.census_enabled,
                    "census_sample_rate": settings.census_sample_rate,
                    "census_time_budget_ms": settings.census_time_budget_ms,
                    "dep_audit_enabled": settings.dep_audit_enabled,
                    "deep_scan_enabled": settings.deep_scan_enabled,
                    "deep_scan_time_budget_ms": settings.deep_scan_time_budget_ms,
                    "deep_scan_interval_samples": settings.deep_scan_interval_samples,
                    "deep_scan_slice_ms": settings.deep_scan_slice_ms,
                    "deep_scan_duty_percent": settings.deep_scan_duty_percent,
                    "proc_smaps_enabled": settings.proc_smaps_enabled,
                    "proc_smaps_min_interval_seconds": (
                        settings.proc_smaps_min_interval_seconds
                    ),
                    "history_size": settings.history_size,
                    "alert_plugin_mb": settings.alert_plugin_mb,
                    "alert_growth_mb_per_hour": settings.alert_growth_mb_per_hour,
                    "alert_rss_mb": settings.alert_rss_mb,
                    "alert_rss_growth_mb_per_hour": settings.alert_rss_growth_mb_per_hour,
                },
                "history": {
                    "samples": self.collector.history.count(),
                    "census_samples": len(
                        self.collector.history.census_samples(),
                    ),
                    "retained_samples": len(
                        self.collector.history.retained_samples(),
                    ),
                    "baseline_at": (
                        self.collector.history.baseline.ts
                        if self.collector.history.baseline
                        else None
                    ),
                },
                "smaps": self.collector.smaps.stats(),
                "routes": self.routes,
                "server_time": time.time(),
            },
        )

    async def get_plugins(self) -> Any:
        params = await _query()
        kwargs: dict[str, Any] = {
            "deep": _as_bool(params.get("deep"), False),
            "record_sample": _as_bool(params.get("sample"), True),
        }
        census = _as_opt_bool(params.get("census"))
        audit = _as_opt_bool(params.get("audit"))
        if census is not None:
            kwargs["census"] = census
        if audit is not None:
            kwargs["audit"] = audit
        report = await self.collector.build_report(**kwargs)
        return ok(report)

    async def get_detail(self) -> Any:
        params = await _query()
        name = (params.get("name") or "").strip()
        if not name:
            return error_response("缺少 name 参数", status_code=400)
        if self.collector.registry.get(name) is None:
            self.collector.ensure_registry(force=True)
            if self.collector.registry.get(name) is None:
                return error_response(f"未找到插件 {name}", status_code=404)
        kwargs: dict[str, Any] = {
            "deep": _as_bool(params.get("deep"), False),
            "detail_for": name,
            "record_sample": False,
        }
        census = _as_opt_bool(params.get("census"))
        audit = _as_opt_bool(params.get("audit"))
        if census is not None:
            kwargs["census"] = census
        if audit is not None:
            kwargs["audit"] = audit
        report = await self.collector.build_report(**kwargs)
        return ok(
            {
                "generated_at": report["generated_at"],
                "detail": report.get("detail"),
                "notes": report.get("notes"),
            },
        )

    async def get_history(self) -> Any:
        params = await _query()
        limit = max(1, min(2000, _as_int(params.get("limit"), 240)))
        plugin = (params.get("plugin") or "").strip()
        history = self.collector.history
        latest = getattr(history, "latest", None)
        rss_delta_fn = getattr(history, "rss_delta", None)
        data: dict[str, Any] = {
            # One packed array per sample instead of five parallel series: the
            # chart draws all of them together and this keeps the payload small
            # enough to poll every few seconds on a 2-core VPS.
            "points": history.chart_points(limit),
            "totals": history.totals_series(limit),
            "rss": history.rss_series(limit),
            "pss": history.pss_series(limit),
            "blocks": history.blocks_series(limit),
            "retained_totals": history.retained_totals_series(limit),
            "census_samples": len(history.census_samples(limit)),
            "retained_samples": len(history.retained_samples(limit)),
            "rss_trend_bytes_per_minute": history.rss_trend_bytes_per_minute(),
            "footprint_trend_bytes_per_minute": (
                history.footprint_trend_bytes_per_minute()
            ),
            "blocks_trend_per_minute": history.blocks_trend_per_minute(),
            "baseline_at": history.baseline.ts if history.baseline else None,
            "baseline_rss_bytes": (
                history.baseline.rss_bytes if history.baseline else None
            ),
            "rss_delta_bytes": (
                rss_delta_fn(latest.rss_bytes)
                if latest is not None and callable(rss_delta_fn)
                else None
            ),
            "interval_seconds": self.collector.settings.sample_interval_seconds,
        }
        coverage = getattr(self.collector, "_last_deep_coverage", None)
        data["coverage"] = coverage
        if plugin:
            data["plugin"] = plugin
            data["series"] = history.series(plugin, limit)
            data["retained_series"] = history.retained_series(plugin, limit)
            data["trend_bytes_per_minute"] = history.trend_bytes_per_minute(plugin)
            data["retained_trend_bytes_per_minute"] = (
                history.retained_trend_bytes_per_minute(plugin)
            )
        else:
            samples = history.samples(limit)
            names: set[str] = set()
            retained_names: set[str] = set()
            for sample in samples:
                names.update(sample.plugins.keys())
                retained_names.update(getattr(sample, "retained", {}).keys())
            data["series_by_plugin"] = {
                name: history.series(name, limit) for name in sorted(names)
            }
            # The table sparkline needs a per-plugin series even when the object
            # census is switched off, which is the default.  Retained samples
            # are already in memory, so this costs one dict walk per plugin.
            data["retained_series_by_plugin"] = {
                name: history.retained_series(name, limit)
                for name in sorted(retained_names)
            }
        return ok(data)

    async def get_alerts(self) -> Any:
        params = await _query()
        limit = max(1, min(200, _as_int(params.get("limit"), 20)))
        return ok(
            {
                "enabled": self.collector.alerts.enabled,
                "alerts": [
                    {
                        "ts": alert.ts,
                        "plugin": alert.plugin,
                        "kind": alert.kind,
                        "message": alert.message,
                        "value": alert.value,
                    }
                    for alert in self.collector.alerts.alerts(limit)
                ],
            },
        )

    async def get_imports(self) -> Any:
        params = await _query()
        report = await self.collector.build_report(
            audit=_as_opt_bool(params.get("audit")),
            record_sample=False,
        )
        return ok(
            {
                "generated_at": report["generated_at"],
                "import_hook": (report.get("process") or {}).get("import_hook"),
                "packages": report.get("packages"),
                "opportunities": report.get("opportunities"),
                "audit_meta": report.get("audit_meta"),
                "totals": report.get("totals"),
                "notes": report.get("notes"),
            },
        )

    async def post_census(self) -> Any:
        """Run one object census on demand.

        Never automatic by default: walking the GC heap touches every tracked
        object, which faults swapped-out pages back in and stalls for as long
        as it takes.  The user asking for it is the consent.
        """

        report = await self.collector.census_now()
        return ok(
            {
                "generated_at": report["generated_at"],
                "census_meta": report.get("census_meta"),
                "census_buckets": report.get("census_buckets"),
                "plugins": report.get("plugins"),
                "totals": report.get("totals"),
                "notes": report.get("notes"),
            },
        )

    async def post_audit(self) -> Any:
        """Re-scan plugin sources for heavy module-level imports."""

        report = await self.collector.audit_now()
        return ok(
            {
                "generated_at": report["generated_at"],
                "audit_meta": report.get("audit_meta"),
                "opportunities": report.get("opportunities"),
                "plugins": report.get("plugins"),
                "totals": report.get("totals"),
                "notes": report.get("notes"),
            },
        )

    async def post_baseline(self) -> Any:
        body = await _body()
        action = str(body.get("action") or "set").strip().lower()
        history = self.collector.history
        if action == "clear":
            history.clear_baseline()
        elif action == "set":
            if history.latest is None:
                await self.collector.build_report(deep=False, record_sample=True)
            history.set_baseline()
        else:
            return error_response("action 必须是 set / clear", status_code=400)
        return ok(
            {
                "action": action,
                "baseline_at": history.baseline.ts if history.baseline else None,
            },
        )

    async def post_gc(self) -> Any:
        return ok(await self.collector.force_gc())
