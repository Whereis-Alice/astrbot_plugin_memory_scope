"""Runtime evidence: privacy, bounds, lifecycle and honest comparisons."""

import asyncio
import inspect
import json
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from core.activity_recorder import ActivityRecorder
from core.allocation_trace import AllocationTrace
from observer.activity import clean_plugin_filter, clean_record
from observer.config import Config
from observer.runtime import Observer
from observer.store import Store


class Client:
    configured = True

    def __init__(self):
        self.sent = []

    async def publish_activities(self, records, status):
        self.sent.extend(records)


def record(uid="a", start=None, **extra):
    now = time.time() if start is None else start
    return dict(
        id=uid,
        start=now,
        end=now + 1,
        category="handler",
        plugin="astrbot_plugin_test",
        operation="run",
        status="ok",
        **extra,
    )


def test_validation_strips_payload_and_rejects_nonfinite():
    now = time.time()
    raw = record(
        start=now - 2, args={"secret": "chat"}, prompt="secret", exception="secret"
    )
    cleaned = clean_record(raw, now)
    assert not {"args", "prompt", "exception"} & cleaned.keys()
    for changes in (
        {"start": float("nan")},
        {"start": True},
        {"end": float("inf")},
        {"category": "unknown"},
        {"id": "../../bad"},
        {"start": now + 50},
    ):
        with pytest.raises(ValueError):
            clean_record(dict(raw, **changes), now)
    assert (
        clean_record(
            dict(raw, allocations=[{"file": "<script>", "line": 2}] * 40), now
        )["allocations"][0]["file"]
        == "_script_"
    )


def test_persistent_upsert_overlap_pagination_and_isolation(tmp_path):
    path = tmp_path / "history.db"
    store = Store(path)
    a = record(start=100)
    store.save_activities("r", [dict(a, end=None, status="running")])
    store.save_activities("r", [dict(a, end=150)])
    store.save_activities("r", [dict(a, end=None, status="running")])
    store.save_activities("other", [record("other", 130)])
    store.save_activities("r", [record("b", 125), record("c", 126)])
    assert store.activities("r", since=145, until=155)["items"][0]["end"] == 150
    first = store.activities("r", limit=2)
    assert [r["id"] for r in first["items"]] == ["c", "b"]
    assert [
        r["id"] for r in store.activities("r", before=first["next_cursor"])["items"]
    ] == ["a"]
    assert not store.activities("r", category="tool")["items"]
    assert not store.activities("r", plugin="missing")["items"]
    store.close()
    reopened = Store(path)
    assert len(reopened.activities("r")["items"]) == 3
    reopened.close()


def test_activity_disk_and_row_limits_do_not_evict_startup(tmp_path):
    store = Store(tmp_path / "history.db")
    store.append("events", "r", {"ts": time.time(), "kind": "launch"})
    store.save_activities("r", [record(str(i)) for i in range(20005)])
    assert store.db.execute("SELECT count(*) FROM activities").fetchone()[0] == 20000
    assert len(store.rows("events", "r")) == 1
    store.max_bytes = 0
    store.save_activities("r", [record("full")])
    assert store.dropped == 1
    store.close()


def test_growth_keeps_actual_samples_and_pid_reuse_distinct(tmp_path):
    observer = Observer(Config(token="a" * 32, state_dir=str(tmp_path)))
    run = {"id": "r", "started_at": 1, "pid": 10, "start_ticks": 5}
    observer.store.save_run(run)
    samples = [
        {
            "ts": 10,
            "memory": {"current": 100},
            "processes": [
                {"pid": 10, "start_ticks": 5, "rss": 50},
                {"pid": 11, "start_ticks": 6, "rss": 20},
            ],
        },
        {"ts": 15, "memory": {"current": 500}},
        {
            "ts": 25,
            "memory": {"current": 160},
            "processes": [
                {"pid": 10, "start_ticks": 5, "rss": 60},
                {"pid": 11, "start_ticks": 7, "rss": 30},
            ],
        },
    ]
    for sample in samples:
        observer.store.append("samples", "r", sample)
    result = observer.growth("r", 5, 30)
    assert result["actual"] == [10, 25]
    assert (
        result["peak"] == 500
        and result["max_gap_seconds"] == 10
        and result["sample_count"] == 3
    )
    assert result["memory"]["current"]["delta"] == 60
    assert result["memory"]["swap"]["delta"] is None
    assert result["processes"][0]["main"] and result["processes"][0]["rss_delta"] == 10
    assert {p["state"] for p in result["processes"][1:]} == {"appeared", "disappeared"}
    assert all(p["rss_delta"] is None for p in result["processes"][1:])
    assert result["causal"] is False
    assert not observer.growth("r", 10, 11)["available"]
    for start, end in [(0, float("inf")), (10, 1), (0, 90000), (float("nan"), 100)]:
        with pytest.raises(ValueError):
            observer.growth("r", start, end)
    observer.close()


def test_wrappers_preserve_results_errors_generator_close_and_privacy():
    async def run():
        recorder = ActivityRecorder(Client())

        async def work(secret):
            return secret

        wrapped = recorder.wrap(work, "plugin", "work")
        assert inspect.iscoroutinefunction(wrapped)
        assert await wrapped("PRIVATE CHAT") == "PRIVATE CHAT"

        async def fail():
            raise RuntimeError("PRIVATE EXCEPTION")

        with pytest.raises(RuntimeError, match="PRIVATE EXCEPTION"):
            await recorder.wrap(fail, "plugin", "fail")()
        closed = []

        async def stream():
            try:
                yield "PRIVATE RESULT"
                yield "second"
            finally:
                closed.append(True)

        wrapper = recorder.wrap(stream, "plugin", "stream")
        assert inspect.isasyncgenfunction(wrapper)
        gen = wrapper()
        assert await anext(gen) == "PRIVATE RESULT"
        await gen.aclose()
        assert closed == [True]
        assert [r["status"] for r in recorder.queue.values()] == [
            "ok",
            "error",
            "cancelled",
        ]
        assert "PRIVATE" not in json.dumps(list(recorder.queue.values()))
        assert not recorder.pending
        await recorder.close()

    asyncio.run(run())


def test_config_caps_pending_expiry_and_bounded_outage():
    assert not ActivityRecorder(Client(), enabled="false").enabled
    assert ActivityRecorder(Client(), max_per_minute=None).max_per_minute == 300
    recorder = ActivityRecorder(Client(), max_per_minute=30)
    for _ in range(40):
        recorder.end(recorder.begin("handler", "plugin", "work"))
    assert len(recorder.queue) == 30 and recorder.dropped == 10
    recorder.max_per_minute = 3000
    for _ in range(600):
        recorder.end(recorder.begin("handler", "plugin", "work"))
    assert len(recorder.queue) == 512
    recorder.begin_key("event", "llm", "AstrBot", "model")
    uid = recorder.keys["event"]
    rec, _ = recorder.pending[uid]
    recorder.pending[uid] = (rec, time.monotonic() - 1801)
    recorder.expire()
    assert not recorder.keys and not recorder.pending
    assert recorder.queue[uid]["status"] == "expired"


def test_flush_does_not_lose_end_arriving_during_network_request():
    async def run():
        client = Client()
        recorder = ActivityRecorder(client)
        uid = recorder.begin("handler", "plugin", "work")

        async def send(records, status):
            client.sent.extend(records)
            recorder.end(uid)

        client.publish_activities = send
        await recorder.flush()
        assert client.sent[0]["status"] == "running"
        assert recorder.queue[uid]["status"] == "ok"
        await recorder.flush()
        assert not recorder.queue

    asyncio.run(run())


def test_instrument_idempotent_replacement_unload_and_restore():
    class Metadata:
        handler_module_path = "plugin.main"
        handler_name = "work"

    async def work():
        return 2

    async def other():
        return 3

    async def run():
        item = Metadata()
        item.handler = work
        stars = {"plugin.main": SimpleNamespace(activated=True, root_dir_name="plugin")}
        recorder = ActivityRecorder(Client())
        recorder.instrument([item], stars)
        replacement = item.handler
        recorder.instrument([item], stars)
        assert item.handler is replacement and len(recorder.patches) == 1
        assert await item.handler() == 2
        item.handler = other
        recorder.instrument([item], stars)
        assert len(recorder.patches) == 1 and await item.handler() == 3
        await recorder.close()
        assert item.handler is other and not recorder.patches
        recorder = ActivityRecorder(Client())
        recorder.instrument([item], stars)
        recorder.instrument([], stars)
        assert not recorder.patches

    asyncio.run(run())


def test_trace_manual_only_refuses_existing_tracer_and_cancels_cleanly():
    async def run():
        recorder = ActivityRecorder(Client())
        trace = AllocationTrace(recorder)
        assert trace.task is None and trace.result == {"state": "idle"}
        assert not tracemalloc.is_tracing()
        trace.start(10)
        await trace.close()  # cancellation even before first task step
        assert trace.result["state"] == "cancelled" and not tracemalloc.is_tracing()
        tracemalloc.start(1)
        try:
            trace.start(10)
            await trace.task
            assert trace.result["state"] == "error" and tracemalloc.is_tracing()
        finally:
            tracemalloc.stop()
        trace.start(10)
        await asyncio.sleep(0)
        assert tracemalloc.is_tracing()
        await trace.close()
        assert not tracemalloc.is_tracing() and trace.result["state"] == "cancelled"
        baseline = trace._start()
        payload = bytearray(200000)
        findings = trace._finish(baseline, True)
        assert any(r["bytes"] >= len(payload) for r in findings)
        assert len(findings) <= 20 and not tracemalloc.is_tracing()
        assert all(not r["file"].startswith(("C:", "/")) for r in findings)

    asyncio.run(run())


def test_authenticated_activity_event_whitelist(monkeypatch, tmp_path):
    from observer import proc

    observer = Observer(
        Config(token="a" * 32, state_dir=str(tmp_path), cgroup=str(tmp_path))
    )
    monkeypatch.setattr(proc, "identity", lambda pid: (0, 10))
    monkeypatch.setattr(proc, "cgroup_pids", lambda group: [1])
    event = {
        "kind": "activity_batch",
        "pid": 1,
        "start_ticks": 10,
        "ts": time.time(),
        "records": [record(start=time.time() - 2, prompt="private")],
        "recorder": {"enabled": True, "handlers": 5},
    }
    assert not observer.accept_event({"token": "bad", "event": event})
    assert observer.accept_event({"token": "a" * 32, "event": event})
    result = observer.activities(observer.current_run["id"])
    assert result["recorder"]["handlers"] == 5
    assert "prompt" not in result["items"][0]
    assert not observer.store.rows("events", observer.current_run["id"])
    inventory = dict(event, kind="inventory", plugins=[], astrbot_loaded=True)
    assert observer.accept_event({"token": "a" * 32, "event": inventory})
    assert observer.accept_event(
        {"token": "a" * 32, "event": dict(inventory, ts=event["ts"] + 1)}
    )
    assert observer.current_run["astrbot_loaded_at"] == event["ts"]
    assert not observer.accept_event(
        {"token": "a" * 32, "event": dict(event, ts=float("nan"))}
    )
    assert not observer.accept_event(
        {"token": "a" * 32, "event": dict(event, records=[record()] * 101)}
    )
    observer.close()


def test_http_activity_authentication_and_interval_validation(tmp_path):
    import threading
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from observer.server import make_server

    observer = Observer(Config(token="a" * 32, state_dir=str(tmp_path), port=0))
    observer.store.save_run({"id": "r", "started_at": 1, "pid": 1, "start_ticks": 1})
    observer.store.save_activities("r", [record("api", 10)])
    server = make_server(observer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(url + "/api/activities?id=r")
        assert error.value.code == 401

        def get(path):
            with urlopen(
                Request(url + path, headers={"Authorization": "Bearer " + "a" * 32})
            ) as response:
                return json.load(response)["data"]

        assert get("/api/activities?id=r")["items"][0]["id"] == "api"
        from urllib.parse import urlencode

        result = get(
            "/api/activities?"
            + urlencode(
                {
                    "id": "r",
                    "plugin_filter": json.dumps(
                        {"mode": "exclude", "plugins": ["astrbot_plugin_test"]}
                    ),
                }
            )
        )
        assert not result["items"] and result["plugin_filter"]["mode"] == "exclude"
        assert not get("/api/growth?id=r&since=1&until=20")["available"]
        for path in (
            "/api/growth?id=r&since=nan&until=20",
            "/api/growth?id=r&since=0&until=90000",
            "/api/activities?id=r&category=unknown",
            "/api/activities?id=r&limit=no",
            "/api/activities?id=r&plugin_filter=oops",
        ):
            with pytest.raises(HTTPError) as error:
                get(path)
            assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        observer.close()


def test_plugin_filter_precedes_pagination_and_does_not_delete_records(tmp_path):
    store = Store(tmp_path / "history.db")
    store.save_activities(
        "r",
        [
            dict(record(str(i), 100 + i), plugin="quiet" if i < 60 else "noisy")
            for i in range(150)
        ],
    )
    store.save_activities("other", [dict(record("other", 500), plugin="quiet")])
    excluded = {"mode": "exclude", "plugins": ["noisy"]}
    page = store.activities("r", plugin_filter=excluded, limit=50)
    assert len(page["items"]) == 50 and all(
        r["plugin"] == "quiet" for r in page["items"]
    )
    next_page = store.activities(
        "r", plugin_filter=excluded, before=page["next_cursor"], limit=50
    )
    assert len(next_page["items"]) == 10 and next_page["next_cursor"] is None
    assert (
        store.activities("r", plugin_filter={"mode": "include", "plugins": []})["items"]
        == []
    )
    assert (
        len(
            store.activities("r", plugin_filter={"mode": "exclude", "plugins": []})[
                "items"
            ]
        )
        == 50
    )
    assert (
        len(
            store.activities(
                "r",
                plugin_filter={"mode": "include", "plugins": ["noisy", "quiet"]},
                limit=200,
            )["items"]
        )
        == 150
    )
    assert (
        store.activities(
            "r", plugin_filter={"mode": "include", "plugins": ["' OR 1=1 --"]}
        )["items"]
        == []
    )
    assert store.activity_plugins("r") == ["noisy", "quiet"]
    assert store.db.execute("SELECT count(*) FROM activities").fetchone()[0] == 151
    store.close()


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        [],
        {"mode": []},
        {"mode": "invalid", "plugins": []},
        {"mode": "include", "plugins": [None]},
        {"mode": "exclude", "plugins": ["a"] * 201},
        {"mode": "exclude", "plugins": ["a" * 121]},
        {"mode": "include", "plugins": ["字" * 100 + str(i) for i in range(100)]},
    ],
)
def test_plugin_filter_bounds(value):
    with pytest.raises(ValueError):
        clean_plugin_filter(value)


def test_activity_memory_uses_neighbouring_samples_and_counts_unfiltered_activity(
    tmp_path,
):
    store = Store(tmp_path / "history.db")
    for ts, value in [(10, 1000), (15, 1500), (20, 1200)]:
        store.append(
            "samples",
            "r",
            {"ts": ts, "memory": {"current": value, "anon": value // 2, "swap": None}},
        )
    items = [
        dict(record("short", 11), end=11.0001, plugin="visible"),
        dict(record("noise", 13), plugin="excluded"),
        dict(record("fall", 16), end=17, plugin="visible"),
    ]
    store.save_activities("r", items)
    store.activity_memory("r", items)
    a = items[0]["memory_window"]
    assert a["available"] and a["actual"] == [10, 15] and a["seconds"] == 5
    assert a["deltas"] == {"current": 500, "anon": 250, "swap": None}
    assert a["activity_count"] == 2 and a["causal"] is False
    assert items[2]["memory_window"]["deltas"]["current"] == -300
    # All tiny callbacks sharing a pair reuse one compact result, not a process tree.
    assert items[0]["memory_window"] is items[1]["memory_window"]
    filtered = store.activities(
        "r", plugin_filter={"mode": "exclude", "plugins": ["excluded"]}
    )["items"]
    store.activity_memory("r", filtered)
    assert (
        next(r for r in filtered if r["id"] == "short")["memory_window"][
            "activity_count"
        ]
        == 2
    )
    missing = [
        dict(record("missing", 100), end=101),
        dict(record("running", 11), end=None),
        record("gap", -100),
    ]
    store.activity_memory("r", missing)
    assert all(not row["memory_window"]["available"] for row in missing)
    store.activity_memory("another-run", items)
    assert all(not row["memory_window"]["available"] for row in items)
    store.close()


def test_activity_memory_waits_for_next_sample_and_rejects_distant_pairs(tmp_path):
    store = Store(tmp_path / "history.db")
    now = time.time()
    store.append("samples", "r", {"ts": now - 1, "memory": {"current": 100}})
    rows = [dict(record("recent", now - 0.5), end=now - 0.1)]
    store.activity_memory("r", rows)
    assert rows[0]["memory_window"]["reason"] == "pending_sample"
    store.append("samples", "r", {"ts": now + 50, "memory": {"current": 200}})
    store.activity_memory("r", rows)
    assert rows[0]["memory_window"]["reason"] == "distant_samples"
    store.close()
