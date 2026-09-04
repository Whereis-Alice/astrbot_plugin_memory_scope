"""RSS/census history, baselines, trends and threshold alerts."""

from __future__ import annotations

from core.sampler import (
    BYTES_PER_MB,
    AlertEngine,
    HistoryStore,
    Sample,
)


def sample(ts: float, value: int, *, rss: int = 100 * BYTES_PER_MB) -> Sample:
    return Sample(
        ts=ts,
        rss_bytes=rss + value,
        census_bytes=value,
        plugins={"demo": value, "other": 1024},
    )


def test_rss_only_sample_is_not_plugin_attribution():
    item = Sample(ts=1.0, rss_bytes=200 * BYTES_PER_MB)

    assert item.has_attribution is False
    assert item.has_retained is False
    assert item.footprint_bytes == 0
    assert item.retained_total_bytes == 0
    assert item.to_payload() == [
        1.0,
        200 * BYTES_PER_MB,
        0,
        {},
        False,
        0,
        0,
        0,
        {},
        False,
    ]


def test_sample_payload_roundtrip_and_type_normalization():
    item = Sample.from_payload([1.25, "2048", "4096", {"demo": "10"}])

    assert item is not None
    assert item.ts == 1.25
    assert item.rss_bytes == 2048
    assert item.census_bytes == 4096
    assert item.plugins == {"demo": 10}
    assert item.census_ran is True

    assert Sample.from_payload([1.0, 2, 3, "not-a-dict"]).plugins == {}
    assert Sample.from_payload(None) is None
    assert Sample.from_payload([1, 2]) is None


def test_history_ring_buffer_and_live_resize():
    history = HistoryStore(3)
    for index in range(5):
        history.add(sample(float(index), index * 100))

    # HistoryStore deliberately keeps a safe minimum of ten samples.
    assert history.count() == 5
    assert history.samples()[0].ts == 0.0

    history.max_samples = 3
    history.replace_samples(history.samples())
    assert [item.ts for item in history.samples()] == [2.0, 3.0, 4.0]


def test_series_and_totals_keep_rss_only_samples_separate():
    history = HistoryStore()
    history.add(sample(1.0, 2048))
    history.add(Sample(ts=2.0, rss_bytes=300 * BYTES_PER_MB))
    history.add(sample(3.0, 4096))

    assert history.series("demo") == [[1.0, 2048], [3.0, 4096]]
    assert history.series("missing") == [[1.0, 0], [3.0, 0]]
    assert history.rss_series() == [
        [1.0, 100 * BYTES_PER_MB + 2048],
        [2.0, 300 * BYTES_PER_MB],
        [3.0, 100 * BYTES_PER_MB + 4096],
    ]
    assert history.totals_series()[1] == [2.0, 300 * BYTES_PER_MB, 0]
    assert len(history.census_samples()) == 2


def test_baseline_and_rss_delta():
    history = HistoryStore()
    first = sample(1.0, 1_000_000, rss=200 * BYTES_PER_MB)
    second = sample(2.0, 1_500_000, rss=220 * BYTES_PER_MB)
    history.add(first)
    history.add(second)

    assert history.delta("demo", 2_000_000) is None
    assert history.set_baseline() is second
    assert history.delta("demo", 2_000_000) == 500_000
    assert history.delta("unknown", 4096) == 4096
    assert history.rss_delta(230 * BYTES_PER_MB) == (
        230 * BYTES_PER_MB - (220 * BYTES_PER_MB + 1_500_000)
    )

    history.clear_baseline()
    assert history.baseline is None
    assert history.delta("demo", 2_000_000) is None


def test_rss_only_baseline_does_not_create_plugin_delta():
    history = HistoryStore()
    baseline = Sample(ts=1.0, rss_bytes=100)
    history.add(baseline)
    history.set_baseline()

    assert history.delta("demo", 1000) is None
    assert history.rss_delta(1200) == 1100


def test_trends_are_least_squares_slopes_in_bytes_per_minute():
    history = HistoryStore()
    for index in range(5):
        history.add(sample(index * 60.0, index * BYTES_PER_MB))

    trend = history.trend_bytes_per_minute("demo")
    assert trend is not None
    assert abs(trend - BYTES_PER_MB) < 1024

    rss_trend = history.rss_trend_bytes_per_minute()
    assert rss_trend is not None
    assert abs(rss_trend - BYTES_PER_MB) < 1024


def test_trends_need_three_points_and_a_time_span():
    history = HistoryStore()
    history.add(sample(1.0, 1))
    history.add(sample(2.0, 2))
    assert history.trend_bytes_per_minute("demo") is None

    same_time = HistoryStore()
    for value in range(3):
        same_time.add(sample(5.0, value))
    assert same_time.trend_bytes_per_minute("demo") is None


def test_payload_roundtrip_and_old_v1_payload_is_rss_only():
    history = HistoryStore()
    history.add(sample(1.0, 4096))
    history.set_baseline()

    payload = history.to_payload(keep=10)
    assert payload["version"] == 3

    restored = HistoryStore()
    restored.load_payload(payload)
    assert restored.latest is not None
    assert restored.latest.census_bytes == 4096
    assert restored.latest.census_ran is True
    assert restored.baseline is not None

    old = {
        "version": 1,
        "samples": [[2.0, 999, 888, {"demo": 777}]],
        "baseline": [2.0, 999, 888, {"demo": 777}],
    }
    migrated = HistoryStore()
    migrated.load_payload(old)
    assert migrated.latest is not None
    assert migrated.latest.rss_bytes == 999
    assert migrated.latest.has_attribution is False
    assert migrated.baseline is not None
    assert migrated.baseline.has_attribution is False


def probe(
    ts: float,
    *,
    rss: int = 100 * BYTES_PER_MB,
    pss: int = 0,
    swap: int = 0,
    blocks: int = 0,
    retained: dict | None = None,
) -> Sample:
    """A sample as the v3 collector builds it: cheap fields plus optional probes."""

    return Sample(
        ts=ts,
        rss_bytes=rss,
        pss_bytes=pss,
        swap_pss_bytes=swap,
        allocated_blocks=blocks,
        retained=dict(retained or {}),
    )


def test_footprint_counts_the_pages_rss_forgot_in_swap():
    resident = 601 * BYTES_PER_MB
    swapped = 366 * BYTES_PER_MB
    item = probe(1.0, rss=resident, pss=resident, swap=swapped)

    # This is the whole point of reading smaps_rollup: RSS says 601 MB while the
    # process is really costing the box 967 MB.
    assert item.footprint_bytes == resident + swapped
    # Without kernel Pss the answer is 0, never a silent fallback to RSS -- a
    # fallback would put an invisible step in the middle of the series.
    assert probe(2.0, rss=resident).footprint_bytes == 0


def test_retained_history_is_kept_apart_from_census_history():
    history = HistoryStore()
    history.add(sample(1.0, 2048))  # census only
    history.add(probe(2.0, retained={"demo": 4096, "other": 1024}))  # retained only
    history.add(probe(3.0))  # neither

    assert len(history.census_samples()) == 1
    assert len(history.retained_samples()) == 1
    # A plugin missing from a retained sample reads 0; samples that never ran the
    # scan are dropped instead, so the series cannot invent a fake recovery.
    assert history.retained_series("demo") == [[2.0, 4096]]
    assert history.retained_series("ghost") == [[2.0, 0]]
    assert history.retained_totals_series() == [[2.0, 5120]]


def test_chart_points_pack_every_series_into_one_row_per_sample():
    history = HistoryStore()
    history.add(probe(1.0, rss=200, pss=190, swap=10, blocks=5000))
    history.add(probe(2.0, rss=210, pss=195, swap=12, blocks=5100, retained={"demo": 64}))
    history.add(sample(3.0, 4096, rss=220))

    points = history.chart_points()
    assert points == [
        [1.0, 200, 190, 10, 5000, 0, 0],
        [2.0, 210, 195, 12, 5100, 64, 0],
        [3.0, 220 + 4096, 0, 0, 0, 0, 4096],
    ]
    # Seven columns, ts first: the chart reads them positionally.
    assert all(len(row) == 7 for row in points)


def test_footprint_and_block_trends_skip_samples_without_the_probe():
    history = HistoryStore()
    for index in range(5):
        history.add(
            probe(
                index * 60.0,
                pss=index * BYTES_PER_MB,
                blocks=10_000 + index * 1000,
            ),
        )
    # Kernel without Pss support in the middle of the window: skipped, not
    # treated as a drop to zero.
    history.add(probe(300.0, rss=999))

    footprint = history.footprint_trend_bytes_per_minute()
    assert footprint is not None
    assert abs(footprint - BYTES_PER_MB) < 1024

    blocks = history.blocks_trend_per_minute()
    assert blocks is not None
    assert abs(blocks - 1000.0) < 1.0


def test_retained_trend_and_delta_need_a_retained_baseline():
    history = HistoryStore()
    for index in range(4):
        history.add(probe(index * 60.0, retained={"demo": index * 1024}))

    trend = history.retained_trend_bytes_per_minute("demo")
    assert trend is not None
    assert abs(trend - 1024.0) < 1.0

    # An RSS-only baseline carries no per-plugin numbers to subtract.
    history.set_baseline(probe(500.0))
    assert history.retained_delta("demo", 9999) is None

    history.set_baseline(probe(600.0, retained={"demo": 1024}))
    assert history.retained_delta("demo", 4096) == 3072
    assert history.retained_delta("ghost", 512) == 512


def test_v3_payload_keeps_footprint_blocks_and_retained():
    history = HistoryStore()
    history.add(probe(9.0, rss=300, pss=280, swap=20, blocks=7777, retained={"demo": 64}))
    history.set_baseline()

    restored = HistoryStore()
    restored.load_payload(history.to_payload(keep=10))
    latest = restored.latest
    assert latest is not None
    assert (latest.pss_bytes, latest.swap_pss_bytes) == (280, 20)
    assert latest.footprint_bytes == 300
    assert latest.allocated_blocks == 7777
    assert latest.retained == {"demo": 64}
    assert latest.has_retained is True

    # v2 payloads stop at census_ran; the retained fields simply come back empty
    # rather than poisoning the series with zeros.
    old = {"version": 2, "samples": [[3.0, 500, 400, {"demo": 128}, True]]}
    migrated = HistoryStore()
    migrated.load_payload(old)
    assert migrated.latest is not None
    assert migrated.latest.has_attribution is True
    assert migrated.latest.has_retained is False
    assert migrated.latest.footprint_bytes == 0


def test_history_ignores_malformed_payloads():
    history = HistoryStore()
    history.load_payload(None)
    history.load_payload("bad")
    history.load_payload({"samples": [None, [], [1, 2], "x", [1, "bad", 2, {}]]})

    assert history.count() == 0
    assert history.baseline is None


def rows(*items):
    return [
        {"name": name, "census_bytes": size, "trend_bytes_per_minute": trend}
        for name, size, trend in items
    ]


def test_alerts_are_disabled_by_default():
    engine = AlertEngine()

    assert engine.enabled is False
    assert engine.evaluate(rows(("demo", 999 * BYTES_PER_MB, 10))) == []
    assert engine.evaluate_process(999 * BYTES_PER_MB, 10) == []
    assert engine.alerts() == []


def test_plugin_size_alert_has_a_cooldown():
    engine = AlertEngine(size_mb=100.0, cooldown_seconds=1800.0)
    payload = rows(("demo", 150 * BYTES_PER_MB, None))

    first = engine.evaluate(payload, now=1000.0)
    assert len(first) == 1
    assert first[0].kind == "size"
    assert first[0].plugin == "demo"
    assert first[0].value == 150.0
    assert engine.evaluate(payload, now=2799.0) == []
    assert len(engine.evaluate(payload, now=2801.0)) == 1


def test_plugin_growth_alert_converts_minute_to_hour():
    engine = AlertEngine(growth_mb_per_hour=30.0)
    fired = engine.evaluate(rows(("demo", 1024, BYTES_PER_MB)), now=5.0)

    assert len(fired) == 1
    assert fired[0].kind == "growth"
    assert abs(fired[0].value - 60.0) < 0.01


def test_plugin_alerts_use_separate_cooldowns_and_ignore_missing_trends():
    engine = AlertEngine(size_mb=1.0, growth_mb_per_hour=1.0)
    fired = engine.evaluate(
        rows(("demo", 2 * BYTES_PER_MB, None)),
        now=10.0,
    )
    assert [item.kind for item in fired] == ["size"]

    later = engine.evaluate(
        rows(("demo", 2 * BYTES_PER_MB, BYTES_PER_MB)),
        now=100.0,
    )
    assert [item.kind for item in later] == ["growth"]


def test_process_rss_alerts_work_without_census():
    engine = AlertEngine(rss_mb=100.0, rss_growth_mb_per_hour=30.0)

    fired = engine.evaluate_process(
        150 * BYTES_PER_MB,
        BYTES_PER_MB,
        now=0.0,
    )
    assert {item.kind for item in fired} == {"rss", "rss_growth"}
    assert {item.plugin for item in fired} == {"__process__"}
    assert engine.enabled is True


def test_alert_thresholds_and_limits_are_clamped():
    engine = AlertEngine(cooldown_seconds=1.0, max_alerts=2)
    assert engine.cooldown_seconds == 60.0

    engine.update_thresholds(10, 20, 30, 40)
    assert engine.plugin_rules_enabled is True
    assert engine.process_rules_enabled is True
    assert engine.rss_mb == 30.0
    assert engine.rss_growth_mb_per_hour == 40.0

    for index in range(4):
        engine.evaluate_process(100 * BYTES_PER_MB, now=1000 + index * 1000)
    assert len(engine.alerts(limit=0)) <= 2
