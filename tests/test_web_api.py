"""Dashboard endpoints for the v2 lightweight probes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core import web_api
from core.collector import Settings


class StubHistory:
    def __init__(self) -> None:
        self.baseline: Any = None
        self.latest: Any = SimpleNamespace(ts=100.0)
        self.calls: list[Any] = []

    def count(self) -> int:
        return 3

    def census_samples(self, limit=None):
        self.calls.append(("census_samples", limit))
        return [SimpleNamespace(ts=1), SimpleNamespace(ts=2)]

    def samples(self, limit=None):
        self.calls.append(("samples", limit))
        return [
            SimpleNamespace(ts=1.0, plugins={"b": 10}),
            SimpleNamespace(ts=2.0, plugins={"a": 20}),
        ]

    def totals_series(self, limit):
        self.calls.append(("totals_series", limit))
        return [[1.0, 100, 20], [2.0, 110, 20]]

    def rss_series(self, limit):
        self.calls.append(("rss_series", limit))
        return [[1.0, 100], [2.0, 110]]

    def series(self, plugin, limit):
        self.calls.append(("series", plugin, limit))
        return [[2.0, 20]]

    def trend_bytes_per_minute(self, plugin):
        self.calls.append(("trend", plugin))
        return 1234.0

    def rss_trend_bytes_per_minute(self):
        self.calls.append(("rss_trend",))
        return 5678.0

    def set_baseline(self):
        self.calls.append("set_baseline")
        self.baseline = SimpleNamespace(ts=42.0)
        return self.baseline

    def clear_baseline(self):
        self.calls.append("clear_baseline")
        self.baseline = None


class StubAlerts:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[Any] = []

    def alerts(self, limit):
        self.calls.append(limit)
        return [
            SimpleNamespace(
                ts=5.0,
                plugin="a_plugin",
                kind="rss",
                message="too big",
                value=99.0,
            ),
        ]


class StubRegistry:
    def __init__(self, known=None, discovered=None):
        self.known = set(known or {"a_plugin"})
        self.discovered = set(discovered or ())

    def get(self, name):
        return SimpleNamespace(name=name) if name in self.known else None


class StubCollector:
    def __init__(self, known=None, discovered=None):
        self.settings = Settings.from_config({})
        self.history = StubHistory()
        self.alerts = StubAlerts()
        self.registry = StubRegistry(known, discovered)
        self.report_calls: list[dict[str, Any]] = []
        self.ensure_calls: list[bool] = []
        self.gc_calls = 0
        self.census_calls = 0
        self.audit_calls = 0

    def process_stats(self, include_object_count=False):
        return {"pid": 4242, "rss_bytes": 1024}

    def import_hook_status(self):
        return {"installed": False, "degraded": False, "overhead_ms": 0}

    def ensure_registry(self, force=False):
        self.ensure_calls.append(force)
        self.registry.known |= self.registry.discovered

    async def build_report(self, **kwargs):
        self.report_calls.append(kwargs)
        name = kwargs.get("detail_for")
        return {
            "generated_at": 777.0,
            "process": {"import_hook": {"installed": False}},
            "plugins": [],
            "packages": [],
            "opportunities": [],
            "audit_meta": None,
            "totals": {},
            "notes": ["census_never_run"],
            "detail": None if name is None else {"name": name, "found": True},
        }

    async def census_now(self):
        self.census_calls += 1
        return {"generated_at": 778.0, "census_meta": {}, "plugins": [], "totals": {}, "notes": []}

    async def audit_now(self):
        self.audit_calls += 1
        return {"generated_at": 779.0, "audit_meta": {}, "opportunities": [], "plugins": [], "totals": {}, "notes": []}

    async def force_gc(self):
        self.gc_calls += 1
        return {"collected": 7}


class FakeRequest:
    def __init__(self, args=None, body=None, awaitable_body=False):
        self.args = dict(args or {})
        self._body = body
        self._awaitable_body = awaitable_body

    def get_json(self, force=False, silent=False):
        if self._awaitable_body:
            async def later():
                return self._body
            return later()
        return self._body


@pytest.fixture()
def api(monkeypatch):
    def fake_json_response(data=None, *, status_code=200, headers=None):
        return {"payload": data, "status_code": status_code}

    def fake_error_response(message, *, status_code=400, data=None, headers=None):
        return {
            "payload": {"status": "error", "message": message, "data": data},
            "status_code": status_code,
        }

    monkeypatch.setattr(web_api, "json_response", fake_json_response)
    monkeypatch.setattr(web_api, "error_response", fake_error_response)
    monkeypatch.setattr(web_api, "_WEB_AVAILABLE", True)

    def build(collector, request=None):
        monkeypatch.setattr(web_api, "request", request)
        return web_api.MemoryScopeWebApi("astrbot_plugin_memory_scope", collector)

    return build


def run(coro):
    return asyncio.run(coro)


def data_of(response):
    assert response["status_code"] == 200
    assert response["payload"]["status"] == "ok"
    return response["payload"]["data"]


def test_helpers_are_defensive(api):
    collector = StubCollector()
    api(collector, None)
    assert run(web_api._query()) == {}
    assert run(web_api._body()) == {}
    assert web_api._as_bool("false") is False
    assert web_api._as_bool("yes") is True
    assert web_api._as_opt_bool("") is None
    assert web_api._as_opt_bool("1") is True
    assert web_api._as_int("7", 2) == 7
    assert web_api._as_int("bad", 2) == 2


def test_register_exposes_v2_routes_and_noops_without_web(api, monkeypatch):
    collector = StubCollector()
    instance = api(collector)
    registered = []

    class Context:
        def register_web_api(self, *args):
            registered.append(args)

    routes = instance.register(Context())
    assert len(routes) == 10
    assert "GET /api/plug/astrbot_plugin_memory_scope/imports" in routes
    assert "POST /api/plug/astrbot_plugin_memory_scope/census" in routes
    assert "POST /api/plug/astrbot_plugin_memory_scope/audit" in routes
    assert len(registered) == 10

    monkeypatch.setattr(web_api, "_WEB_AVAILABLE", False)
    assert instance.register(Context()) == []


def test_overview_contains_process_settings_and_history(api):
    collector = StubCollector()
    instance = api(collector)
    instance.routes = ["GET /api/plug/x/overview"]

    payload = data_of(run(instance.get_overview()))
    assert payload["process"]["pid"] == 4242
    assert payload["settings"]["measure_import_cost"] is True
    assert payload["history"] == {"samples": 3, "census_samples": 2, "baseline_at": None}
    assert payload["routes"] == instance.routes
    assert payload["server_time"] > 0


def test_plugins_passes_query_options(api):
    collector = StubCollector()
    instance = api(
        collector,
        FakeRequest(args={"deep": "1", "census": "true", "audit": "0", "sample": "0"}),
    )
    payload = data_of(run(instance.get_plugins()))
    assert payload["generated_at"] == 777.0
    assert collector.report_calls == [
        {"deep": True, "census": True, "audit": False, "record_sample": False},
    ]


def test_detail_validation_refresh_and_options(api):
    collector = StubCollector(known=set(), discovered={"late_plugin"})
    instance = api(collector, FakeRequest(args={"name": "late_plugin", "deep": "1", "census": "1"}))
    payload = data_of(run(instance.get_detail()))
    assert payload["detail"]["name"] == "late_plugin"
    assert collector.ensure_calls == [True]
    assert collector.report_calls == [
        {
            "deep": True,
            "detail_for": "late_plugin",
            "census": True,
            "record_sample": False,
        },
    ]

    missing = api(StubCollector(), FakeRequest(args={"name": " "}))
    response = run(missing.get_detail())
    assert response["status_code"] == 400

    unknown = api(StubCollector(), FakeRequest(args={"name": "ghost"}))
    response = run(unknown.get_detail())
    assert response["status_code"] == 404


def test_history_supports_process_and_plugin_series(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"limit": "10"}))
    payload = data_of(run(instance.get_history()))
    assert payload["totals"] == [[1.0, 100, 20], [2.0, 110, 20]]
    assert payload["rss"] == [[1.0, 100], [2.0, 110]]
    assert payload["rss_trend_bytes_per_minute"] == 5678.0
    assert set(payload["series_by_plugin"]) == {"a", "b"}

    one = api(collector, FakeRequest(args={"plugin": "a", "limit": "5"}))
    one_payload = data_of(run(one.get_history()))
    assert one_payload["series"] == [[2.0, 20]]
    assert one_payload["trend_bytes_per_minute"] == 1234.0


def test_alerts_limit_is_clamped_and_serialized(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"limit": "0"}))
    payload = data_of(run(instance.get_alerts()))
    assert collector.alerts.calls == [1]
    assert payload["enabled"] is True
    assert payload["alerts"][0]["kind"] == "rss"


def test_imports_and_manual_actions(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"audit": "1"}))
    imports = data_of(run(instance.get_imports()))
    assert imports["generated_at"] == 777.0
    assert collector.report_calls[-1] == {"audit": True, "record_sample": False}

    census = data_of(run(instance.post_census()))
    assert census["generated_at"] == 778.0
    assert collector.census_calls == 1
    audit = data_of(run(instance.post_audit()))
    assert audit["generated_at"] == 779.0
    assert collector.audit_calls == 1


def test_baseline_set_clear_and_bad_action(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(body={}))
    assert data_of(run(instance.post_baseline()))["baseline_at"] == 42.0
    assert "set_baseline" in collector.history.calls

    clear = api(collector, FakeRequest(body={"action": "clear"}))
    assert data_of(run(clear.post_baseline()))["baseline_at"] is None

    bad = api(collector, FakeRequest(body={"action": "reset"}))
    response = run(bad.post_baseline())
    assert response["status_code"] == 400


def test_gc_delegates_to_collector(api):
    collector = StubCollector()
    instance = api(collector)
    assert data_of(run(instance.post_gc())) == {"collected": 7}
    assert collector.gc_calls == 1
