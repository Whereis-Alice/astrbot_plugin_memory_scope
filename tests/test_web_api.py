"""Dashboard endpoints: argument parsing, error paths and payload shapes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core import web_api
from core.collector import Settings


# ----------------------------------------------------------------------
# stubs
# ----------------------------------------------------------------------
class StubProbe:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.tracing = False

    def start(self) -> bool:
        self.calls.append("start")
        self.tracing = True
        return True

    def stop(self, only_if_started_by_plugin: bool = True) -> bool:
        self.calls.append(("stop", only_if_started_by_plugin))
        self.tracing = False
        return True

    def reset_peak(self) -> None:
        self.calls.append("reset_peak")

    def status(self) -> dict[str, Any]:
        return {"tracing": self.tracing, "frames": 4}


class StubHistory:
    def __init__(self) -> None:
        self.baseline: Any = None
        self.latest: Any = SimpleNamespace(ts=100.0)
        self.calls: list[Any] = []
        self._samples = [
            SimpleNamespace(ts=1.0, plugins={"b_plugin": 10, "a_plugin": 20}),
            SimpleNamespace(ts=2.0, plugins={"a_plugin": 30}),
        ]

    def count(self) -> int:
        return len(self._samples)

    def samples(self, limit: int) -> list[Any]:
        self.calls.append(("samples", limit))
        return self._samples[-limit:]

    def totals_series(self, limit: int) -> list[Any]:
        self.calls.append(("totals_series", limit))
        return [[1.0, 30], [2.0, 30]]

    def series(self, plugin: str, limit: int) -> list[Any]:
        self.calls.append(("series", plugin, limit))
        return [[2.0, 30]]

    def trend_bytes_per_minute(self, plugin: str) -> float:
        self.calls.append(("trend", plugin))
        return 1234.0

    def set_baseline(self) -> None:
        self.calls.append("set_baseline")
        self.baseline = SimpleNamespace(ts=42.0)

    def clear_baseline(self) -> None:
        self.calls.append("clear_baseline")
        self.baseline = None


class StubAlerts:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[Any] = []

    def alerts(self, limit: int) -> list[Any]:
        self.calls.append(limit)
        return [
            SimpleNamespace(
                ts=5.0,
                plugin="a_plugin",
                kind="size",
                message="too big",
                value=99.0,
            ),
        ]


class StubRegistry:
    def __init__(self, known: set[str], discovered: set[str] | None = None) -> None:
        self.known = set(known)
        self.discovered = set(discovered or ())

    def get(self, name: str) -> Any:
        if name in self.known:
            return SimpleNamespace(name=name)
        return None

    def refresh(self) -> None:  # pragma: no cover - not used directly
        self.known |= self.discovered


class StubCollector:
    def __init__(
        self,
        known: set[str] | None = None,
        discovered: set[str] | None = None,
    ) -> None:
        self.settings = Settings.from_config({})
        self.probe = StubProbe()
        self.history = StubHistory()
        self.alerts = StubAlerts()
        self.registry = StubRegistry(known if known is not None else {"a_plugin"}, discovered)
        self.report_calls: list[dict[str, Any]] = []
        self.ensure_calls: list[bool] = []
        self.gc_calls = 0

    def process_stats(self) -> dict[str, Any]:
        return {"pid": 4242, "rss_bytes": 1024}

    def ensure_registry(self, force: bool = False) -> None:
        self.ensure_calls.append(force)
        self.registry.known |= self.registry.discovered

    async def build_report(self, **kwargs: Any) -> dict[str, Any]:
        self.report_calls.append(kwargs)
        name = kwargs.get("detail_for")
        return {
            "generated_at": 777.0,
            "plugins": [],
            "notes": ["tracemalloc_off"],
            "detail": None if name is None else {"name": name, "found": True},
        }

    async def force_gc(self) -> dict[str, Any]:
        self.gc_calls += 1
        return {"collected": 7}


class FakeRequest:
    """Minimal stand-in for the quart request object."""

    def __init__(
        self,
        args: dict[str, str] | None = None,
        body: Any = None,
        *,
        awaitable_body: bool = False,
    ) -> None:
        self.args = dict(args or {})
        self._body = body
        self._awaitable_body = awaitable_body

    def get_json(self, force: bool = False, silent: bool = False) -> Any:
        if self._awaitable_body:

            async def _later() -> Any:
                return self._body

            return _later()
        return self._body


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch):
    """Patch the response helpers into plain dicts so assertions stay readable."""

    def fake_json_response(
        data: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {"payload": data, "status_code": status_code}

    def fake_error_response(
        message: str,
        *,
        status_code: int = 400,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "payload": {"status": "error", "message": message, "data": data},
            "status_code": status_code,
        }

    monkeypatch.setattr(web_api, "json_response", fake_json_response)
    monkeypatch.setattr(web_api, "error_response", fake_error_response)
    monkeypatch.setattr(web_api, "_WEB_AVAILABLE", True)

    def _build(collector: StubCollector, request: FakeRequest | None = None):
        monkeypatch.setattr(web_api, "request", request)
        return web_api.MemoryScopeWebApi("astrbot_plugin_memory_scope", collector)

    return _build


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def data_of(response: dict[str, Any]) -> Any:
    assert response["status_code"] == 200
    assert response["payload"]["status"] == "ok"
    return response["payload"]["data"]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def test_as_bool_accepts_the_usual_truthy_spellings():
    assert web_api._as_bool(None) is False
    assert web_api._as_bool(None, True) is True
    assert web_api._as_bool(True) is True
    assert web_api._as_bool("0") is False
    assert web_api._as_bool("false") is False
    assert web_api._as_bool("off") is False
    assert web_api._as_bool("") is False
    assert web_api._as_bool(" 1 ") is True
    assert web_api._as_bool("TRUE") is True
    assert web_api._as_bool("Yes") is True
    assert web_api._as_bool("on") is True


def test_as_int_falls_back_on_garbage():
    assert web_api._as_int("25", 5) == 25
    assert web_api._as_int(" 7 ", 5) == 7
    assert web_api._as_int(None, 5) == 5
    assert web_api._as_int("abc", 5) == 5
    assert web_api._as_int("1.5", 5) == 5


def test_query_and_body_are_defensive(api):
    collector = StubCollector()

    instance = api(collector, None)
    assert run(web_api._query()) == {}
    assert run(web_api._body()) == {}

    instance = api(collector, FakeRequest(args={"deep": "1", "limit": "3"}))
    assert run(web_api._query()) == {"deep": "1", "limit": "3"}

    instance = api(collector, FakeRequest(body={"action": "set"}))
    assert run(web_api._body()) == {"action": "set"}

    instance = api(collector, FakeRequest(body={"action": "gc"}, awaitable_body=True))
    assert run(web_api._body()) == {"action": "gc"}

    instance = api(collector, FakeRequest(body="not-a-dict"))
    assert run(web_api._body()) == {}
    assert instance.plugin_name == "astrbot_plugin_memory_scope"


# ----------------------------------------------------------------------
# registration
# ----------------------------------------------------------------------
def test_register_publishes_eight_routes(api):
    collector = StubCollector()
    instance = api(collector)
    registered: list[tuple[Any, ...]] = []

    class FakeContext:
        def register_web_api(self, route, handler, methods, desc):
            registered.append((route, handler, tuple(methods), desc))

    routes = instance.register(FakeContext())

    assert routes == [
        "GET /api/plug/astrbot_plugin_memory_scope/overview",
        "GET /api/plug/astrbot_plugin_memory_scope/plugins",
        "GET /api/plug/astrbot_plugin_memory_scope/detail",
        "GET /api/plug/astrbot_plugin_memory_scope/history",
        "GET /api/plug/astrbot_plugin_memory_scope/alerts",
        "POST /api/plug/astrbot_plugin_memory_scope/tracing",
        "POST /api/plug/astrbot_plugin_memory_scope/baseline",
        "POST /api/plug/astrbot_plugin_memory_scope/gc",
    ]
    assert instance.routes == routes
    assert len(registered) == 8
    assert registered[0][0] == "/astrbot_plugin_memory_scope/overview"
    assert registered[0][1] == instance.get_overview
    assert all(callable(entry[1]) for entry in registered)
    assert all(entry[3].startswith("MemoryScope") for entry in registered)


def test_register_is_a_noop_without_a_web_framework(api, monkeypatch):
    collector = StubCollector()
    instance = api(collector)
    monkeypatch.setattr(web_api, "_WEB_AVAILABLE", False)

    class ExplodingContext:
        def register_web_api(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("must not register while unavailable")

    assert instance.register(ExplodingContext()) == []
    assert instance.available is False
    assert instance.routes == []


# ----------------------------------------------------------------------
# GET handlers
# ----------------------------------------------------------------------
def test_overview_exposes_process_settings_and_routes(api):
    collector = StubCollector()
    instance = api(collector)
    instance.routes = ["GET /api/plug/x/overview"]

    payload = data_of(run(instance.get_overview()))

    assert payload["process"]["pid"] == 4242
    assert payload["settings"]["sample_interval_seconds"] == 60
    assert payload["settings"]["deep_scan_enabled"] is True
    assert payload["settings"]["history_size"] == 720
    assert payload["history"] == {"samples": 2, "baseline_at": None}
    assert payload["routes"] == ["GET /api/plug/x/overview"]
    assert payload["server_time"] > 0
    # No report is built for the cheap overview call.
    assert collector.report_calls == []


def test_overview_reports_the_baseline_timestamp(api):
    collector = StubCollector()
    collector.history.baseline = SimpleNamespace(ts=123.5)
    instance = api(collector)

    assert data_of(run(instance.get_overview()))["history"]["baseline_at"] == 123.5


def test_plugins_defaults_to_a_shallow_recorded_sample(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest())

    data_of(run(instance.get_plugins()))

    assert collector.report_calls == [{"deep": False, "record_sample": True}]


def test_plugins_honours_deep_and_sample_flags(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"deep": "1", "sample": "0"}))

    payload = data_of(run(instance.get_plugins()))

    assert collector.report_calls == [{"deep": True, "record_sample": False}]
    assert payload["generated_at"] == 777.0


def test_detail_requires_a_name(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"name": "  "}))

    response = run(instance.get_detail())

    assert response["status_code"] == 400
    assert "name" in response["payload"]["message"]
    assert collector.report_calls == []


def test_detail_returns_404_for_an_unknown_plugin(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"name": "ghost"}))

    response = run(instance.get_detail())

    assert response["status_code"] == 404
    assert "ghost" in response["payload"]["message"]
    # A forced refresh is attempted before giving up.
    assert collector.ensure_calls == [True]
    assert collector.report_calls == []


def test_detail_refreshes_the_registry_for_freshly_loaded_plugins(api):
    collector = StubCollector(known=set(), discovered={"late_plugin"})
    instance = api(collector, FakeRequest(args={"name": "late_plugin", "limit": "5"}))

    payload = data_of(run(instance.get_detail()))

    assert collector.ensure_calls == [True]
    assert payload["detail"] == {"name": "late_plugin", "found": True}
    assert payload["generated_at"] == 777.0
    assert payload["notes"] == ["tracemalloc_off"]
    assert set(payload) == {"generated_at", "detail", "notes"}


def test_detail_passes_deep_and_line_limit_through(api):
    collector = StubCollector()
    instance = api(
        collector,
        FakeRequest(args={"name": "a_plugin", "deep": "true", "limit": "40"}),
    )

    data_of(run(instance.get_detail()))

    assert collector.report_calls == [
        {
            "deep": True,
            "detail_for": "a_plugin",
            "line_limit": 40,
            "record_sample": False,
        },
    ]
    assert collector.ensure_calls == []


def test_detail_limit_falls_back_to_25(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"name": "a_plugin", "limit": "many"}))

    data_of(run(instance.get_detail()))

    assert collector.report_calls[0]["line_limit"] == 25


def test_history_without_a_plugin_returns_every_series(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest())

    payload = data_of(run(instance.get_history()))

    assert payload["totals"] == [[1.0, 30], [2.0, 30]]
    assert payload["interval_seconds"] == 60
    assert payload["baseline_at"] is None
    # Sorted so the chart legend is stable across refreshes.
    assert list(payload["series_by_plugin"]) == ["a_plugin", "b_plugin"]
    assert "series" not in payload
    assert ("totals_series", 240) in collector.history.calls


def test_history_for_one_plugin_adds_the_trend(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"plugin": "a_plugin", "limit": "10"}))

    payload = data_of(run(instance.get_history()))

    assert payload["plugin"] == "a_plugin"
    assert payload["series"] == [[2.0, 30]]
    assert payload["trend_bytes_per_minute"] == 1234.0
    assert "series_by_plugin" not in payload
    assert ("series", "a_plugin", 10) in collector.history.calls


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 1), ("-5", 1), ("99999", 2000), ("nope", 240)],
)
def test_history_limit_is_clamped(api, raw, expected):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"limit": raw}))

    data_of(run(instance.get_history()))

    assert ("totals_series", expected) in collector.history.calls


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 1), ("500", 200), ("bad", 20), ("15", 15)],
)
def test_alerts_limit_is_clamped(api, raw, expected):
    collector = StubCollector()
    instance = api(collector, FakeRequest(args={"limit": raw}))

    payload = data_of(run(instance.get_alerts()))

    assert collector.alerts.calls == [expected]
    assert payload["enabled"] is True
    assert payload["alerts"][0] == {
        "ts": 5.0,
        "plugin": "a_plugin",
        "kind": "size",
        "message": "too big",
        "value": 99.0,
    }


# ----------------------------------------------------------------------
# POST handlers
# ----------------------------------------------------------------------
def test_tracing_start_stop_and_reset_peak(api):
    collector = StubCollector()

    instance = api(collector, FakeRequest(body={"action": "start"}))
    payload = data_of(run(instance.post_tracing()))
    assert payload == {"action": "start", "tracemalloc": {"tracing": True, "frames": 4}}

    instance = api(collector, FakeRequest(body={"action": " STOP "}))
    payload = data_of(run(instance.post_tracing()))
    assert payload["action"] == "stop"
    assert payload["tracemalloc"]["tracing"] is False

    instance = api(collector, FakeRequest(body={"action": "reset_peak"}))
    data_of(run(instance.post_tracing()))

    # An explicit dashboard stop wins even when tracing was started elsewhere.
    assert collector.probe.calls == ["start", ("stop", False), "reset_peak"]


@pytest.mark.parametrize("body", [{}, {"action": ""}, {"action": "explode"}, None])
def test_tracing_rejects_unknown_actions(api, body):
    collector = StubCollector()
    instance = api(collector, FakeRequest(body=body))

    response = run(instance.post_tracing())

    assert response["status_code"] == 400
    assert "start" in response["payload"]["message"]
    assert collector.probe.calls == []


def test_baseline_defaults_to_set(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(body={}))

    payload = data_of(run(instance.post_baseline()))

    assert payload == {"action": "set", "baseline_at": 42.0}
    assert "set_baseline" in collector.history.calls
    # A sample already exists, so no report has to be built first.
    assert collector.report_calls == []


def test_baseline_samples_once_when_history_is_empty(api):
    collector = StubCollector()
    collector.history.latest = None
    instance = api(collector, FakeRequest(body={"action": "set"}))

    data_of(run(instance.post_baseline()))

    assert collector.report_calls == [{"deep": False, "record_sample": True}]
    assert "set_baseline" in collector.history.calls


def test_baseline_clear_drops_the_reference(api):
    collector = StubCollector()
    collector.history.baseline = SimpleNamespace(ts=9.0)
    instance = api(collector, FakeRequest(body={"action": "clear"}))

    payload = data_of(run(instance.post_baseline()))

    assert payload == {"action": "clear", "baseline_at": None}
    assert collector.history.calls == ["clear_baseline"]


def test_baseline_rejects_unknown_actions(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(body={"action": "reset"}))

    response = run(instance.post_baseline())

    assert response["status_code"] == 400
    assert "set" in response["payload"]["message"]
    assert collector.history.calls == []


def test_gc_delegates_to_the_collector(api):
    collector = StubCollector()
    instance = api(collector, FakeRequest(body={}))

    assert data_of(run(instance.post_gc())) == {"collected": 7}
    assert collector.gc_calls == 1
