"""Windowing, evidence fidelity and bounded payload regression tests."""

import pytest

from observer.config import Config
from observer.runtime import Observer


@pytest.fixture
def observer(tmp_path):
    instance = Observer(Config(token="x" * 32, state_dir=str(tmp_path), port=0))
    yield instance
    instance.close()


def test_window_uses_end_of_historical_run_and_strips_process_trees(observer):
    run = {"id": "old", "started_at": 10, "ended_at": 1000, "inventory": []}
    observer.store.save_run(run)
    for ts in (50, 100, 500, 1000):
        observer.store.append(
            "samples",
            "old",
            {
                "ts": ts,
                "memory": {"current": ts},
                "processes": [{"pid": 1}],
                "host": {"available": 99},
            },
        )
    result = observer.trend("old", 900)
    assert [p["ts"] for p in result["samples"]] == [100, 500, 1000]
    assert result["source_points"] == 3
    assert all(set(p) == {"ts", "memory"} for p in result["samples"])
    assert not result["downsampled"]
    assert [
        p["ts"] for p in observer.store.rows("samples", "old", until=100, compact=True)
    ] == [50, 100]
    with pytest.raises(ValueError):
        observer.trend("missing")
    with pytest.raises(ValueError):
        observer.store.rows("unsafe", "old")


def test_downsampling_keeps_each_metric_peak_endpoints_and_missing_values(observer):
    observer.store.save_run({"id": "long", "started_at": 0, "ended_at": 3000})
    for i in range(3001):
        m = {"current": 100, "anon_swap": 50, "swap": 5}
        if i == 999:
            m["current"] = 10000
        if i == 777:
            m["anon_swap"] = 8888
        if i == 2222:
            m["swap"] = 900
        if i == 1266:
            m["current"] = None
        observer.store.append(
            "samples", "long", {"ts": i, "memory": m, "state": "running"}
        )
    result = observer.trend("long", 0)
    points = result["samples"]
    assert result["source_points"] == 3001 and result["downsampled"]
    assert len(points) <= 1980
    assert points[0]["ts"] == 0 and points[-1]["ts"] == 3000
    by_ts = {p["ts"]: p for p in points}
    assert by_ts[999]["memory"]["current"] == 10000
    assert by_ts[777]["memory"]["anon_swap"] == 8888
    assert by_ts[2222]["memory"]["swap"] == 900
    assert by_ts[1266]["memory"]["current"] is None
    assert list(by_ts) == sorted(by_ts)


def test_lightweight_startup_report_preserves_phase_evidence(observer):
    observer.store.save_run({"id": "r", "started_at": 100, "inventory": []})
    for ts, value in ((100, 100), (101, 200), (10000, 300)):
        observer.store.append(
            "samples", "r", {"ts": ts, "memory": {"anon_swap": value}}
        )
    observer.store.append(
        "events",
        "r",
        {
            "ts": 100,
            "seq": 1,
            "kind": "phase_start",
            "plugin": "p",
            "phase": "import",
            "counters": {"VmRSS": 100, "VmSwap": 0},
            "monotonic_ns": 0,
        },
    )
    observer.store.append(
        "events",
        "r",
        {
            "ts": 101,
            "seq": 2,
            "span": 1,
            "kind": "phase_end",
            "plugin": "p",
            "phase": "import",
            "counters": {"VmRSS": 300, "VmSwap": 0},
            "monotonic_ns": 1000000000,
        },
    )
    full = observer.report("r")
    light = observer.report("r", include_samples=False)
    assert "samples" not in light
    assert light == {k: v for k, v in full.items() if k != "samples"}
    assert light["phases"][0]["service_anon_swap_delta"] == 100
