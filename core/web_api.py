"""Dashboard Web API backing pages/memory/index.html.

AstrBot already gates ``/api/plug/...`` behind the dashboard session, so these
routes inherit the dashboard authentication and add no auth of their own.  Every
handler is read-only except the explicit tracing/baseline/GC actions.
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
        if not _WEB_AVAILABLE:
            return []
        endpoints: list[tuple[str, Callable[..., Any], list[str], str]] = [
            ("overview", self.get_overview, ["GET"], "MemoryScope 进程概览"),
            ("plugins", self.get_plugins, ["GET"], "MemoryScope 各插件内存占用"),
            ("detail", self.get_detail, ["GET"], "MemoryScope 单插件分配明细"),
            ("history", self.get_history, ["GET"], "MemoryScope 采样历史"),
            ("alerts", self.get_alerts, ["GET"], "MemoryScope 告警记录"),
            ("tracing", self.post_tracing, ["POST"], "MemoryScope 开关 tracemalloc"),
            ("baseline", self.post_baseline, ["POST"], "MemoryScope 基线管理"),
            ("gc", self.post_gc, ["POST"], "MemoryScope 手动触发 GC"),
        ]
        registered: list[str] = []
        for suffix, handler, methods, desc in endpoints:
            route = f"/{self.plugin_name}/{suffix}"
            context.register_web_api(route, handler, methods, desc)
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
                    "auto_start_tracemalloc": settings.auto_start_tracemalloc,
                    "auto_start_min_available_mb": settings.auto_start_min_available_mb,
                    "deep_scan_enabled": settings.deep_scan_enabled,
                    "deep_scan_time_budget_ms": settings.deep_scan_time_budget_ms,
                    "history_size": settings.history_size,
                    "alert_plugin_mb": settings.alert_plugin_mb,
                    "alert_growth_mb_per_hour": settings.alert_growth_mb_per_hour,
                },
                "history": {
                    "samples": self.collector.history.count(),
                    "traced_samples": len(
                        self.collector.history.traced_samples(),
                    ),
                    "baseline_at": (
                        self.collector.history.baseline.ts
                        if self.collector.history.baseline
                        else None
                    ),
                },
                "routes": self.routes,
                "server_time": time.time(),
            },
        )

    async def get_plugins(self) -> Any:
        params = await _query()
        deep = _as_bool(params.get("deep"), False)
        report = await self.collector.build_report(
            deep=deep,
            record_sample=_as_bool(params.get("sample"), True),
        )
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
        report = await self.collector.build_report(
            deep=_as_bool(params.get("deep"), False),
            detail_for=name,
            line_limit=_as_int(params.get("limit"), 25),
            record_sample=False,
        )
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
        data: dict[str, Any] = {
            "totals": history.totals_series(limit),
            "traced_samples": len(history.traced_samples(limit)),
            "baseline_at": history.baseline.ts if history.baseline else None,
            "interval_seconds": self.collector.settings.sample_interval_seconds,
        }
        if plugin:
            data["plugin"] = plugin
            data["series"] = history.series(plugin, limit)
            data["trend_bytes_per_minute"] = history.trend_bytes_per_minute(plugin)
        else:
            samples = history.samples(limit)
            names: set[str] = set()
            for sample in samples:
                names.update(sample.plugins.keys())
            data["series_by_plugin"] = {
                name: history.series(name, limit) for name in sorted(names)
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

    async def post_tracing(self) -> Any:
        body = await _body()
        action = str(body.get("action") or "").strip().lower()
        probe = self.collector.probe
        if action == "start":
            probe.start()
        elif action == "stop":
            probe.stop(only_if_started_by_plugin=False)
        elif action == "reset_peak":
            probe.reset_peak()
        else:
            return error_response(
                "action 必须是 start / stop / reset_peak",
                status_code=400,
            )
        return ok({"action": action, "tracemalloc": probe.status()})

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