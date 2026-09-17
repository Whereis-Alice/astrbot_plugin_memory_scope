"""Control-plane regression tests: no real services or data are modified."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from observer.config import Config
from observer.experiments import Experiments
from observer.store import Store


@pytest.fixture
def observer(tmp_path):
    config = Config(
        token="test-only-token-" * 3,
        state_dir=str(tmp_path),
        allow_experiments=True,
        settle_seconds=5,
        observe_seconds=5,
    )
    store = Store(tmp_path / "history.db")
    import threading

    instance = SimpleNamespace(
        config=config,
        store=store,
        current_run={"id": "original"},
        stop=threading.Event(),
        latest={"ts": 0},
    )
    yield instance
    store.close()


def job():
    return {
        "id": "job-1",
        "started_at": time.time(),
        "plugin": "target",
        "mode": "skip",
        "rounds": 1,
        "state": "queued",
        "runs": [],
        "fingerprint": "same",
    }


def test_successful_experiment_uses_transient_plan_and_restores_normal(
    observer, monkeypatch
):
    control = Experiments(observer)
    labels = []

    def restart():
        import json

        plan = json.loads(observer.config.plan_path.read_text())
        labels.append(plan["label"])
        n = len(labels)
        observer.current_run = {
            "id": str(n),
            "ready_at": 100 + n,
            "probe_finished_at": 100 + n,
            "plan": plan,
            "plan_applied": plan["mode"] == "skip",
            "fingerprint": "same",
        }
        observer.latest = {"ts": 1000}

    observer.summary = lambda run_id: {
        "run_id": run_id,
        "median": 100 if run_id != "2" else 70,
        "range": [100, 100] if run_id != "2" else [70, 70],
        "complete": True,
        "fingerprint": "same",
    }
    monkeypatch.setattr(control, "restart", restart)
    record = job()
    control.execute(record)
    assert labels == ["A", "B", "A"]
    assert record["state"] == "complete"
    assert record["result"]["saving_bytes"] == 30
    import json

    assert json.loads(observer.config.plan_path.read_text())["consumed"]


def test_b_failure_requests_recovery_and_reports_it(observer, monkeypatch):
    control = Experiments(observer)
    calls = []

    def restart():
        import json

        plan = json.loads(observer.config.plan_path.read_text())
        calls.append(plan["mode"])
        if plan["mode"] == "skip":
            raise RuntimeError("injected B failure")
        observer.current_run = {
            "id": str(len(calls)),
            "ready_at": 100,
            "probe_finished_at": 100,
            "plan": plan,
            "fingerprint": "same",
        }
        observer.latest = {"ts": 1000}

    observer.summary = lambda _: {
        "run_id": "1",
        "median": 100,
        "range": [100, 100],
        "complete": True,
        "fingerprint": "same",
    }
    monkeypatch.setattr(control, "restart", restart)
    record = job()
    control.execute(record)
    assert calls == ["normal", "skip", "normal"]
    assert record["state"] == "failed"
    assert record["restoration"] == "normal_ready"


def test_experiment_requires_explicit_restart_and_known_target(observer):
    control = Experiments(observer)
    with pytest.raises(ValueError, match="确认"):
        control.create({"plugin": "target"})
    with pytest.raises(ValueError, match="清单"):
        control.create({"plugin": "target", "confirm_restart": True})
    with pytest.raises(ValueError, match="观测插件"):
        control.create(
            {"plugin": "astrbot_plugin_memory_scope", "confirm_restart": True}
        )
    observer.config.cgroup = "/sys/fs/cgroup/container"
    with pytest.raises(ValueError, match="仅支持观察"):
        control.create({"plugin": "target", "confirm_restart": True})


def test_pending_experiment_is_not_silently_resumed(observer):
    record = job()
    record["state"] = "running"
    observer.store.save_job(record)
    control = Experiments(observer)
    assert control.worker is None
    saved = observer.store.jobs()[0]
    assert saved["state"] == "interrupted"


def test_probe_finish_after_adaptation_failure_does_not_enable_experiments(observer):
    observer.current_run.update(
        probe_enabled=True,
        probe_finished_at=time.time(),
        fingerprint="same",
        inventory=[{"root_dir_name": "target", "activated": True}],
    )
    for seq, kind in enumerate(("probe_installed", "probe_error", "probe_finished"), 1):
        observer.store.append(
            "events", "original", {"seq": seq, "kind": kind, "ts": time.time()}
        )
    control = Experiments(observer)
    with pytest.raises(ValueError, match="不完整或适配失败"):
        control.create({"plugin": "target", "confirm_restart": True})
    assert control.worker is None


def test_cancelled_job_before_first_restart_does_not_restart(observer, monkeypatch):
    control = Experiments(observer)
    control.cancelled.set()

    def forbidden():
        raise AssertionError("must not restart")

    monkeypatch.setattr(control, "restart", forbidden)
    record = job()
    control.execute(record)
    assert record["state"] == "cancelled"
