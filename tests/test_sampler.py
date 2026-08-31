"""History ring buffer, baselines, trends and threshold alerts."""

from __future__ import annotations

from core.sampler import BYTES_PER_MB, AlertEngine, HistoryStore, Sample


def make_history(points, max_samples=720):
    """points: list of (ts, plugin_bytes) for a single plugin named demo."""

    history = HistoryStore(max_samples)
    for ts, value in points:
        history.add(
            Sample(
                ts=ts,
                rss_bytes=100 * BYTES_PER_MB + value,
                traced_bytes=value * 2,
                plugins={"demo": value, "other": 1024},
            ),
        )
    return history


def test_empty_history():
    history = HistoryStore()

    assert history.count() == 0
    assert history.latest is None
    assert history.baseline is None
    assert history.samples() == []
    assert history.series("demo") == []
    assert history.delta("demo", 4096) is None
    assert history.trend_bytes_per_minute("demo") is None
    assert history.set_baseline() is None


def test_ring_buffer_drops_oldest_samples():
    history = make_history([(float(i), i * 1024) for i in range(40)], max_samples=10)

    assert history.count() == 10
    assert history.latest is not None
    assert history.latest.ts == 39.0
    assert history.samples()[0].ts == 30.0
    assert history.samples(limit=3)[0].ts == 37.0


def test_max_samples_has_a_floor():
    assert HistoryStore(1).max_samples == 10
    assert HistoryStore(0).max_samples == 10


def test_series_and_totals_series():
    history = make_history([(1.0, 2048), (2.0, 4096)])

    assert history.series("demo") == [[1.0, 2048], [2.0, 4096]]
    assert history.series("missing") == [[1.0, 0], [2.0, 0]]
    assert history.series("demo", limit=1) == [[2.0, 4096]]

    totals = history.totals_series()
    assert totals[0][0] == 1.0
    assert totals[0][2] == 4096
    assert len(totals) == 2


def test_baseline_delta():
    history = make_history([(1.0, 1_000_000), (2.0, 1_500_000)])

    assert history.delta("demo", 2_000_000) is None

    baseline = history.set_baseline()
    assert baseline is not None
    assert baseline.ts == 2.0
    assert history.delta("demo", 2_000_000) == 500_000
    assert history.delta("demo", 1_000_000) == -500_000
    assert history.delta("unknown_plugin", 4096) == 4096

    history.clear_baseline()
    assert history.baseline is None
    assert history.delta("demo", 2_000_000) is None


def test_set_baseline_accepts_an_explicit_sample():
    history = make_history([(1.0, 100), (2.0, 200)])
    pinned = Sample(ts=0.5, rss_bytes=0, traced_bytes=0, plugins={"demo": 50})

    assert history.set_baseline(pinned) is pinned
    assert history.delta("demo", 250) == 200


def test_trend_needs_three_points():
    assert make_history([(0.0, 0), (60.0, 1024)]).trend_bytes_per_minute("demo") is None
    assert make_history([(0.0, 0)]).trend_bytes_per_minute("demo") is None


def test_trend_is_bytes_per_minute():
    # 1 MB every 60 seconds -> 1 MB/min.
    points = [(float(i) * 60.0, i * BYTES_PER_MB) for i in range(5)]

    trend = make_history(points).trend_bytes_per_minute("demo")

    assert trend is not None
    assert abs(trend - BYTES_PER_MB) < 1024


def test_trend_is_negative_when_memory_is_released():
    points = [(float(i) * 60.0, (5 - i) * BYTES_PER_MB) for i in range(5)]

    trend = make_history(points).trend_bytes_per_minute("demo")

    assert trend is not None
    assert trend < 0


def test_trend_without_time_span():
    history = make_history([(7.0, 10), (7.0, 20), (7.0, 30)])

    assert history.trend_bytes_per_minute("demo") is None


def test_trend_window_only_uses_recent_points():
    points = [(float(i) * 60.0, 0) for i in range(10)]
    points += [(float(10 + i) * 60.0, i * BYTES_PER_MB) for i in range(5)]

    history = make_history(points)
    windowed = history.trend_bytes_per_minute("demo", window=5)
    overall = history.trend_bytes_per_minute("demo", window=15)

    assert windowed is not None and overall is not None
    assert windowed > overall


def test_payload_roundtrip():
    history = make_history([(1.0, 4096), (2.0, 8192)])
    history.set_baseline()

    payload = history.to_payload(keep=2)
    assert payload["version"] == 1
    assert len(payload["samples"]) == 2
    assert payload["baseline"] is not None

    restored = HistoryStore()
    restored.load_payload(payload)

    assert restored.count() == 2
    assert restored.latest is not None
    assert restored.latest.plugins["demo"] == 8192
    assert restored.baseline is not None
    assert restored.baseline.ts == 2.0
    assert restored.delta("demo", 10_000) == 1808


def test_payload_keep_limit_and_no_baseline():
    history = make_history([(float(i), i) for i in range(10)])

    payload = history.to_payload(keep=3)

    assert len(payload["samples"]) == 3
    assert payload["baseline"] is None


def test_load_payload_ignores_garbage():
    history = HistoryStore()

    history.load_payload(None)
    history.load_payload("nonsense")
    history.load_payload({"samples": [None, [], [1, 2], "x", [1.0, "a", 2, {}]]})

    assert history.count() == 0
    assert history.baseline is None


def test_sample_payload_normalizes_types():
    sample = Sample.from_payload([1.5, "2048", "4096", {"demo": "10"}])

    assert sample is not None
    assert sample.rss_bytes == 2048
    assert sample.traced_bytes == 4096
    assert sample.plugins == {"demo": 10}

    sample_without_plugins = Sample.from_payload([1.0, 1, 2, "not-a-dict"])
    assert sample_without_plugins is not None
    assert sample_without_plugins.plugins == {}


def rows(*specs):
    return [
        {"name": name, "attributed_bytes": size, "trend_bytes_per_minute": trend}
        for name, size, trend in specs
    ]


def test_alerts_disabled_by_default():
    engine = AlertEngine()

    assert engine.enabled is False
    assert engine.evaluate(rows(("demo", 999 * BYTES_PER_MB, 10 * BYTES_PER_MB))) == []
    assert engine.alerts() == []


def test_size_alert_fires_once_per_cooldown():
    engine = AlertEngine(size_mb=100.0, cooldown_seconds=1800.0)
    payload = rows(("demo", 150 * BYTES_PER_MB, None))

    first = engine.evaluate(payload, now=1000.0)
    assert len(first) == 1
    assert first[0].kind == "size"
    assert first[0].plugin == "demo"
    assert abs(first[0].value - 150.0) < 0.01
    assert "150.0 MB" in first[0].message

    assert engine.evaluate(payload, now=1000.0 + 1799.0) == []

    later = engine.evaluate(payload, now=1000.0 + 1801.0)
    assert len(later) == 1
    assert len(engine.alerts()) == 2


def test_size_alert_ignores_smaller_plugins():
    engine = AlertEngine(size_mb=100.0)

    fired = engine.evaluate(
        rows(("small", 99 * BYTES_PER_MB, None), ("big", 100 * BYTES_PER_MB, None)),
        now=10.0,
    )

    assert [alert.plugin for alert in fired] == ["big"]


def test_growth_alert_uses_bytes_per_minute():
    # 1 MB/min == 60 MB/hour, threshold is 30 MB/hour.
    engine = AlertEngine(growth_mb_per_hour=30.0)

    fired = engine.evaluate(rows(("demo", 1024, BYTES_PER_MB)), now=5.0)

    assert len(fired) == 1
    assert fired[0].kind == "growth"
    assert abs(fired[0].value - 60.0) < 0.01


def test_growth_alert_ignores_missing_or_slow_trends():
    engine = AlertEngine(growth_mb_per_hour=30.0)

    assert engine.evaluate(rows(("demo", 1024, None)), now=5.0) == []
    assert engine.evaluate(rows(("demo", 1024, 1024)), now=6.0) == []
    assert engine.evaluate(rows(("", 1024, BYTES_PER_MB)), now=7.0) == []


def test_size_and_growth_are_separate_cooldowns():
    engine = AlertEngine(size_mb=1.0, growth_mb_per_hour=1.0)

    fired = engine.evaluate(rows(("demo", 10 * BYTES_PER_MB, BYTES_PER_MB)), now=100.0)

    assert sorted(alert.kind for alert in fired) == ["growth", "size"]


def test_cooldown_has_a_floor_and_thresholds_are_updatable():
    engine = AlertEngine(cooldown_seconds=1.0)
    assert engine.cooldown_seconds == 60.0

    engine.update_thresholds(50.0, 0.0)
    assert engine.enabled is True
    assert engine.size_mb == 50.0

    engine.update_thresholds(0.0, 0.0)
    assert engine.enabled is False


def test_alerts_limit_returns_the_most_recent():
    engine = AlertEngine(size_mb=1.0, cooldown_seconds=60.0)
    for index in range(5):
        engine.evaluate(
            rows(("plugin_" + str(index), 10 * BYTES_PER_MB, None)),
            now=1000.0 + index,
        )

    assert len(engine.alerts()) == 5
    recent = engine.alerts(limit=2)
    assert [alert.plugin for alert in recent] == ["plugin_3", "plugin_4"]
    assert len(engine.alerts(limit=0)) == 5
