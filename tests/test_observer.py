from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
from types import ModuleType, SimpleNamespace
from urllib import error, request

import pytest

from observer import proc
from observer.analysis import compare, startup_report, summarize_run
from observer.config import Config
from observer.launch import consume_plan
from observer.probe import EventSink, StartupProbe
from observer.store import Store


def test_config_rejects_unsafe_or_excessive_sampling(tmp_path):
    good = Config(token="a" * 32, state_dir=str(tmp_path))
    good.validate()
    for changes in (
        {"interval": 0.01},
        {"host": "0.0.0.0"},
        {"unit": "bot;id.service"},
        {"token": "short"},
        {"allow_experiments": "false"},
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(good, **changes).validate()
    assert "token" not in good.public()


def test_kernel_missing_swap_is_not_zero(tmp_path):
    (tmp_path / "memory.stat").write_text("anon 4096\nfile 8192\n")
    (tmp_path / "memory.current").write_text("16384")
    (tmp_path / "memory.max").write_text("max")
    measured = proc.cgroup_memory(tmp_path)
    assert measured["anon_swap"] is None
    assert measured["limit"] is None
    (tmp_path / "memory.swap.current").write_text("2048")
    assert proc.cgroup_memory(tmp_path)["anon_swap"] == 6144


def test_process_identity_handles_spaces_and_parentheses(tmp_path):
    root = tmp_path / "12"
    root.mkdir()
    tail = ["S", "10", *(["0"] * 17), "987"]
    (root / "stat").write_text("12 (a (special) name) " + " ".join(tail))
    assert proc.identity(12, tmp_path) == (10, 987)


def test_single_launch_plan_consumption(tmp_path):
    path = tmp_path / "plan.json"
    plan = {"mode": "skip", "plugin": "x", "expires_at": time.time() + 20}
    path.write_text(json.dumps(plan))
    assert consume_plan(path)["plugin"] == "x"
    assert consume_plan(path) == {}
    path.write_text(json.dumps(dict(plan, expires_at=0)))
    assert consume_plan(path) == {}
    for invalid in (
        [],
        None,
        {"expires_at": None},
        dict(plan, plugin=[]),
        dict(plan, expires_at=float("nan")),
    ):
        path.write_text(json.dumps(invalid))
        assert consume_plan(path) == {}


def test_history_bounded_and_run_identity_preserved(tmp_path):
    store = Store(tmp_path / "history.db", max_samples=5)
    try:
        now = time.time()
        store.save_run({"id": "boot:10:100", "started_at": now})
        store.save_run({"id": "boot:10:200", "started_at": now + 1})
        for i in range(10):
            store.append("samples", "boot:10:100", {"ts": now + i, "value": i})
        store.prune()
        assert len(store.rows("samples", "boot:10:100")) == 5
        assert len(store.runs()) == 2
        assert store.run("nonexistent") is None
    finally:
        store.close()


def test_diagnostic_snapshot_round_trips_in_persistent_store(tmp_path):
    store = Store(tmp_path / "history.db")
    snapshot = {
        "generated_at": 123.0,
        "source": "automatic",
        "plugins": [{"name": "example", "retained_bytes": 4096}],
    }
    try:
        store.save_diagnostic("run-1", snapshot, 124.0)
        assert store.diagnostic("run-1") == {"ts": 124.0, "snapshot": snapshot}
        assert store.latest_diagnostic() == {
            "run_id": "run-1",
            "ts": 124.0,
            "snapshot": snapshot,
        }
        assert store.diagnostic("missing") is None
    finally:
        store.close()


def event(kind, seq, ts, **extra):
    return dict(kind=kind, seq=seq, ts=ts, monotonic_ns=int(ts * 1e9), **extra)


def test_phase_missing_samples_and_negative_delta_are_honest():
    run = {"id": "r", "started_at": 0, "inventory": [{"name": "earlier"}]}
    events = [
        event(
            "phase_start",
            1,
            1,
            plugin="a",
            phase="import",
            counters={"VmRSS": 200, "VmSwap": 100},
        ),
        event(
            "phase_end",
            2,
            1.001,
            plugin="a",
            phase="import",
            span=1,
            counters={"VmRSS": 150, "VmSwap": 100},
        ),
        event("probe_finished", 3, 2),
    ]
    report = startup_report(run, [{"ts": 1, "memory": {"anon_swap": 300}}], events)
    assert report["phases"][0]["process_rss_swap_delta"] == -50
    assert report["phases"][0]["service_anon_swap_delta"] is None
    assert report["probe_complete"]
    assert (
        next(p for p in report["plugins"] if p["plugin"] == "earlier")["phases"] == []
    )
    report = startup_report(run, [], [events[0], events[2]])
    assert not report["probe_complete"]
    assert report["missing_events"] == 1


def test_comparison_rejects_environment_drift_and_sparse_windows():
    a = {
        "run_id": "a",
        "complete": True,
        "median": 100,
        "range": [95, 105],
        "fingerprint": "x",
    }
    b = dict(a, run_id="b", median=70, range=[68, 72])
    result = compare({"A": [a], "B": [b]})
    assert result["saving_bytes"] == 30
    assert result["classification"] == "single_trial"
    result = compare({"A": [a, dict(a, run_id="a2")], "B": [b, dict(b, run_id="b2")]})
    assert result["classification"] == "difference_observed"
    assert (
        compare({"A": [a], "B": [dict(b, fingerprint="y")]})["classification"]
        == "environment_changed"
    )
    assert compare({"A": [a], "B": [dict(b, complete=False)]})["saving_bytes"] is None
    with pytest.raises(ValueError):
        compare({"A": [a], "B": [a]})
    summary = summarize_run(
        {"id": "r", "started_at": 0, "ready_at": 1},
        [{"ts": 6, "memory": {"anon_swap": 1}}],
        5,
        30,
    )
    assert summary["median"] is None


class Sink:
    def __init__(self):
        self.events = []
        self.overhead_ns = 0
        self.closed = False

    def emit(self, kind, **kw):
        self.events.append(dict(kind=kind, **kw))
        return len(self.events)

    def begin(self, plugin, phase):
        return self.emit("phase_start", plugin=plugin, phase=phase)

    def end(self, plugin, phase, span, failed=False, **kw):
        self.emit(
            "phase_end", plugin=plugin, phase=phase, span=span, failed=failed, **kw
        )

    def close(self):
        self.closed = True


@pytest.mark.parametrize("mode", ["normal", "skip", "disable"])
def test_probe_covers_first_plugin_restores_hooks_and_never_persists(mode):
    imported, initialized = [], []

    async def global_get(key, default=None):
        return []

    sp = SimpleNamespace(global_get=global_get)

    class Star:
        async def initialize(self):
            initialized.append(type(self).__module__)

    class Manager:
        def _get_plugin_modules(self):
            return [
                {"pname": x, "module": "main", "reserved": False}
                for x in ("first", "second")
            ]

        async def _import_plugin_with_dependency_recovery(self, *, root_dir_name):
            imported.append(root_dir_name)
            module = ModuleType("data.plugins." + root_dir_name + ".main")
            module.Plugin = type("Plugin", (Star,), {"__module__": module.__name__})
            return module

        async def reload(self):
            disabled = await sp.global_get("inactivated_plugins", [])
            for item in self._get_plugin_modules():
                module = await self._import_plugin_with_dependency_recovery(
                    root_dir_name=item["pname"]
                )
                if module.__name__ not in disabled:
                    await module.Plugin().initialize()
            return True

    original = Manager.reload
    sink = Sink()
    probe = StartupProbe(sink, {"mode": mode, "plugin": "first"})
    probe.patch_manager(SimpleNamespace(PluginManager=Manager, sp=sp))
    assert asyncio.run(Manager().reload())
    assert Manager.reload is original
    assert sp.global_get is global_get
    assert sink.closed
    assert imported == (["second"] if mode == "skip" else ["first", "second"])
    assert len(initialized) == (2 if mode == "normal" else 1)
    phases = [e for e in sink.events if e["kind"] == "phase_start"]
    assert phases[0]["plugin"] == ("second" if mode == "skip" else "first")


def test_failed_plugin_still_restores_probe():
    class Manager:
        def _get_plugin_modules(self):
            return []

        async def _import_plugin_with_dependency_recovery(self, **kwargs):
            raise RuntimeError("bad plugin")

        async def reload(self):
            return await self._import_plugin_with_dependency_recovery(
                root_dir_name="bad"
            )

    original = Manager.reload
    sink = Sink()
    probe = StartupProbe(sink)
    probe.patch_manager(SimpleNamespace(PluginManager=Manager))
    with pytest.raises(RuntimeError):
        asyncio.run(Manager().reload())
    assert Manager.reload is original
    assert any(e.get("failed") for e in sink.events)


def test_event_sink_missing_backend_does_not_block():
    sink = EventSink(1, "x" * 32, max_events=2)
    started = time.monotonic()
    for _ in range(10):
        sink.emit("test")
    sink.close()
    assert sink.dropped >= 8
    assert time.monotonic() - started < 1


def test_child_creation_has_context_and_popen_is_restored():
    import subprocess
    import sys

    original = subprocess.Popen.__init__
    sink = Sink()
    probe = StartupProbe(sink)
    probe.install()
    context_token = probe.active_plugin.set("example")
    try:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
    finally:
        probe.active_plugin.reset(context_token)
        probe.finish()
    assert subprocess.Popen.__init__ is original
    events = [e for e in sink.events if e["kind"] == "child_spawn"]
    assert len(events) == 1 and events[0]["plugin"] == "example"
    assert events[0]["child_pid"] == child.pid


def test_launch_invalid_monitor_config_does_not_break_bot(tmp_path):
    import subprocess
    import sys

    target = tmp_path / "bot.py"
    target.write_text("print('bot-still-starts')\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "observer",
            "launch",
            "--config",
            str(tmp_path / "missing.json"),
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    assert "bot-still-starts" in result.stdout


def test_http_requires_token_and_never_returns_secret(tmp_path):
    from observer.runtime import Observer
    from observer.server import make_server

    config = Config(token="test-secret-" * 4, state_dir=str(tmp_path), port=0)
    observer = Observer(config)
    run = {
        "id": "diagnostic-run",
        "started_at": time.time(),
        "pid": 1,
        "start_ticks": 1,
    }
    observer.current_run = run
    observer.store.save_run(run)
    observer.store.save_diagnostic(
        run["id"],
        {
            "generated_at": 123.0,
            "source": "automatic",
            "plugins": [{"name": "example", "retained_bytes": 4096}],
        },
        124.0,
    )
    observer.current_run = None
    assert observer.diagnostics()["run_id"] == run["id"]
    observer.current_run = run
    server = make_server(observer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:" + str(server.server_port)
    try:
        with pytest.raises(error.HTTPError) as exc:
            request.urlopen(url + "/api/overview")
        assert exc.value.code == 401
        req = request.Request(
            url + "/api/overview", headers={"Authorization": "Bearer " + config.token}
        )
        raw = request.urlopen(req).read().decode()
        assert config.token not in raw
        assert json.loads(raw)["data"]["source"] == "independent_observer"
        req = request.Request(
            url + "/api/diagnostics",
            headers={"Authorization": "Bearer " + config.token},
        )
        diagnostics = json.loads(request.urlopen(req).read())
        assert diagnostics["data"]["snapshot"]["source"] == "automatic"
        req = request.Request(
            url + "/api/experiments",
            data=b'{"confirm_restart":true}',
            headers={
                "Authorization": "Bearer " + config.token,
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(error.HTTPError) as exc:
            request.urlopen(req)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        observer.close()


def test_partial_probe_install_failure_restores_wrappers_and_starts_bot(
    tmp_path, monkeypatch
):
    import subprocess
    import sys

    import observer.launch as launcher

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(dataclasses.asdict(Config(token="t" * 32, state_dir=str(tmp_path))))
    )
    target = tmp_path / "bot.py"
    target.write_text(
        "from pathlib import Path\nPath('started.txt').write_text('ok')\n"
    )
    original = subprocess.Popen.__init__
    install = StartupProbe.install

    def broken(probe):
        install(probe)
        raise RuntimeError("injected failure after patching")

    sink = Sink()
    monkeypatch.setattr(launcher, "EventSink", lambda *_: sink)
    monkeypatch.setattr(StartupProbe, "install", broken)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", sys.argv[:])
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.delenv("MEMORYSCOPE_EARLY_PROBE", raising=False)
    monkeypatch.delenv("MEMORYSCOPE_OBSERVER_CONFIG", raising=False)
    launcher.launch(str(config), str(target), [])
    assert (tmp_path / "started.txt").read_text() == "ok"
    assert subprocess.Popen.__init__ is original
    assert not any(
        type(f).__qualname__.startswith("StartupProbe.") for f in sys.meta_path
    )
    assert any(e["kind"] == "probe_error" for e in sink.events)
    assert sink.closed


def test_bridge_failure_marks_cached_overview_stale(monkeypatch):
    from core.observer_client import ObserverClient

    client = ObserverClient(
        {"observer_url": "http://127.0.0.1:1", "observer_token": "x" * 32}
    )
    client.cache["overview{}"] = (
        time.monotonic() - 100,
        {"stale": False, "latest": {"ts": 1}},
    )

    def fail(*args):
        raise ValueError("unreachable")

    monkeypatch.setattr(client, "_request", fail)
    result = asyncio.run(client.get("overview"))
    assert result["stale"] and result["latest"]["ts"] == 1
    assert result["bridge_cache_age_seconds"] >= 100
