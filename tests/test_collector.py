"""End-to-end payload build with a fake AstrBot context."""

from __future__ import annotations

import asyncio
import importlib.util
import tracemalloc
import types
from types import SimpleNamespace

import pytest

from core.collector import (
    MemoryCollector,
    Settings,
    autostart_memory_block_reason,
    available_memory_mb,
)

PLUGIN_ID = "astrbot_plugin_memory_scope"


class FakeContext:
    """Only the pieces of astrbot Context that MemoryScope actually touches."""

    def __init__(self, metas):
        self._metas = metas
        self.cached_config = {"unrelated": "core state"}

    def get_all_stars(self):
        return list(self._metas)


def make_plugin(tmp_path, name, *, source="CACHE = []\n", payload_size=0):
    plugin_dir = tmp_path / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    entry_file = plugin_dir / "main.py"
    entry_file.write_text(source, encoding="utf-8")

    module = types.ModuleType(name + "_module")
    module.__file__ = str(entry_file)
    if payload_size:
        module.CACHE = [bytearray(payload_size)]

    star = SimpleNamespace(state={"buffer": bytearray(payload_size or 1024)})
    meta = SimpleNamespace(
        name=name,
        root_dir_name=name,
        module=module,
        star_cls=star,
        module_path=name + ".main",
        display_name=name.replace("_", " ").title(),
        version="0.9.0",
        author="tester",
        activated=True,
        reserved=False,
    )
    return meta, module, star


def build_collector(metas, **config):
    settings = Settings.from_config(config)
    return MemoryCollector(FakeContext(metas), settings, PLUGIN_ID)


def test_report_shape_without_tracing(tmp_path):
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already started by the environment")

    meta, _module, _star = make_plugin(tmp_path, "plugin_demo", payload_size=40_000)
    collector = build_collector([meta], deep_scan_enabled=False)

    report = asyncio.run(collector.build_report(deep=True, record_sample=True))

    assert set(report) >= {
        "generated_at",
        "process",
        "plugins",
        "others",
        "totals",
        "deep_meta",
        "history",
        "notes",
        "self_plugin",
    }
    assert report["self_plugin"] == PLUGIN_ID

    process = report["process"]
    assert process["pid"] > 0
    assert process["threads"] >= 1
    assert process["tracemalloc"]["tracing"] is False
    assert set(process["gc"]) >= {"counts", "thresholds", "collections", "uncollectable"}

    # No snapshot means no attribution, which the UI has to be told about.
    assert "tracemalloc_off" in report["notes"]
    assert "no_snapshot" in report["notes"]
    assert report["totals"]["traced_bytes"] == 0
    assert report["others"] == []

    row = report["plugins"][0]
    assert row["name"] == "plugin_demo"
    assert row["display_name"] == "Plugin Demo"
    assert row["version"] == "0.9.0"
    assert row["attributed_bytes"] == 0
    assert row["delta_bytes"] is None
    assert row["trend_bytes_per_minute"] is None
    assert row["is_self"] is False
    # deep_scan_enabled=False must veto the requested deep scan.
    assert row["retained"] is None
    assert report["deep_meta"]["fresh"] is False

    # The RSS trend is recorded even without tracing (the default), but the
    # sample carries no per-plugin numbers.
    assert report["history"]["samples"] == 1
    assert report["history"]["traced_samples"] == 0


def test_deep_scan_measures_retained_memory(tmp_path):
    meta, _module, _star = make_plugin(tmp_path, "plugin_heavy", payload_size=120_000)
    collector = build_collector(
        [meta],
        deep_scan_enabled=True,
        deep_scan_time_budget_ms=5000,
    )

    report = asyncio.run(collector.build_report(deep=True, record_sample=False))
    row = report["plugins"][0]

    assert report["deep_meta"]["fresh"] is True
    assert report["deep_meta"]["generated_at"] is not None
    assert row["retained"] is not None
    assert row["retained"]["exclusive_bytes"] >= 120_000
    assert row["retained"]["total_bytes"] >= row["retained"]["exclusive_bytes"]

    # A follow-up shallow report reuses the previous scan and says so.
    reused = asyncio.run(collector.build_report(deep=False, record_sample=False))
    assert reused["deep_meta"]["fresh"] is False
    assert reused["plugins"][0]["retained"] == row["retained"]


def test_core_objects_are_not_billed_to_plugins(tmp_path):
    """Objects reachable only through the context belong to AstrBot, not to us."""

    meta, module, _star = make_plugin(tmp_path, "plugin_light", payload_size=1024)
    context_blob = bytearray(500_000)
    context = FakeContext([meta])
    context.cached_config = {"blob": context_blob}
    module.CORE_REF = context_blob

    collector = MemoryCollector(context, Settings.from_config({}), PLUGIN_ID)
    report = asyncio.run(collector.build_report(deep=True, record_sample=False))

    assert report["plugins"][0]["retained"]["total_bytes"] < 500_000


def test_attributes_real_allocations_and_detail(tmp_path):
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already started by the environment")

    source = (
        "BUFFERS = []\n"
        "\n"
        "\n"
        "def fill(count, size):\n"
        "    BUFFERS.append([bytearray(size) for _ in range(count)])\n"
        "    return len(BUFFERS)\n"
    )
    meta, _module, _star = make_plugin(tmp_path, "plugin_live", source=source)

    spec = importlib.util.spec_from_file_location(
        "plugin_live_main",
        meta.module.__file__,
    )
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    meta.module = live
    meta.star_cls = SimpleNamespace(module=live)

    collector = build_collector([meta], deep_scan_enabled=False)
    collector.probe.start()
    try:
        live.fill(64, 8192)
        report = asyncio.run(
            collector.build_report(
                deep=False,
                detail_for="plugin_live",
                record_sample=True,
            ),
        )
    finally:
        collector.probe.stop()

    assert "no_snapshot" not in report["notes"]
    assert "tracing_started_late" in report["notes"]
    assert report["totals"]["traced_bytes"] > 0
    assert report["totals"]["measured_plugin_count"] == 1
    assert report["others"], "non-plugin allocations should be bucketed"

    row = report["plugins"][0]
    assert row["name"] == "plugin_live"
    assert row["attributed_bytes"] >= 64 * 8192
    assert row["direct_bytes"] > 0
    assert row["blocks"] > 0
    assert 0 < row["traced_share"] <= 100

    detail = report["detail"]
    assert detail["found"] is True
    assert detail["name"] == "plugin_live"
    assert detail["row"] is row
    hotspots = detail["lines"]
    assert hotspots
    assert hotspots[0]["filename"] == meta.module.__file__
    assert hotspots[0]["bytes"] > 0
    assert hotspots[0]["blocks"] > 0

    # The sample was recorded, so the plugin now has a history entry.
    assert report["history"]["samples"] == 1
    assert collector.history.series("plugin_live")[-1][1] == row["attributed_bytes"]


def test_detail_for_unknown_plugin(tmp_path):
    meta, _module, _star = make_plugin(tmp_path, "plugin_demo")
    collector = build_collector([meta], deep_scan_enabled=False)

    report = asyncio.run(
        collector.build_report(detail_for="nope", record_sample=False),
    )

    assert report["detail"]["found"] is False
    assert report["detail"]["row"] is None
    assert report["detail"]["lines"] == []
    assert report["detail"]["series"] == []


def test_rows_are_sorted_and_self_is_flagged(tmp_path):
    small, _m1, _s1 = make_plugin(tmp_path, "plugin_small", payload_size=2048)
    big, _m2, _s2 = make_plugin(tmp_path, "plugin_big", payload_size=200_000)
    mine, _m3, _s3 = make_plugin(tmp_path, PLUGIN_ID, payload_size=4096)

    collector = build_collector([small, big, mine], deep_scan_enabled=False)
    report = asyncio.run(collector.build_report(record_sample=False))

    names = [row["name"] for row in report["plugins"]]
    assert set(names) == {"plugin_small", "plugin_big", PLUGIN_ID}
    assert report["totals"]["plugin_count"] == 3
    assert [row["is_self"] for row in report["plugins"] if row["name"] == PLUGIN_ID] == [True]

    attributed = [row["attributed_bytes"] for row in report["plugins"]]
    assert attributed == sorted(attributed, reverse=True)


def test_sample_once_returns_alerts(tmp_path):
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already started by the environment")

    source = (
        "BUFFERS = []\n"
        "\n"
        "\n"
        "def fill(count, size):\n"
        "    BUFFERS.append([bytearray(size) for _ in range(count)])\n"
        "    return len(BUFFERS)\n"
    )
    meta, _module, _star = make_plugin(tmp_path, "plugin_hungry", source=source)
    spec = importlib.util.spec_from_file_location(
        "plugin_hungry_main",
        meta.module.__file__,
    )
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    meta.module = live

    collector = build_collector(
        [meta],
        deep_scan_enabled=False,
        alert_plugin_mb=0.5,
    )
    collector.probe.start()
    try:
        live.fill(128, 8192)
        alerts = asyncio.run(collector.sample_once())
    finally:
        collector.probe.stop()

    assert len(alerts) == 1
    assert alerts[0].plugin == "plugin_hungry"
    assert alerts[0].kind == "size"
    assert collector.history.count() == 1
    assert collector.last_alerts == alerts


def test_force_gc_reports_numbers(tmp_path):
    collector = build_collector([], deep_scan_enabled=False)

    result = asyncio.run(collector.force_gc())

    assert set(result) == {
        "collected",
        "traced_before",
        "traced_after",
        "freed_bytes",
        "uncollectable",
    }
    assert result["collected"] >= 0
    assert result["freed_bytes"] >= 0


def test_apply_settings_updates_live_components(tmp_path):
    collector = build_collector([], deep_scan_enabled=False)

    collector.apply_settings(
        Settings.from_config(
            {
                "tracemalloc_frames": 25,
                "history_size": 100,
                "alert_plugin_mb": 64.0,
                "alert_growth_mb_per_hour": 8.0,
            },
        ),
    )

    assert collector.probe.frames == 25
    assert collector.history.max_samples == 100
    assert collector.alerts.size_mb == 64.0
    assert collector.alerts.growth_mb_per_hour == 8.0
    assert collector.alerts.enabled is True


def test_object_count_is_optional(tmp_path):
    collector = build_collector([], include_object_count=True, deep_scan_enabled=True)

    with_count = asyncio.run(collector.build_report(deep=True, record_sample=False))
    without_count = asyncio.run(collector.build_report(deep=False, record_sample=False))

    assert with_count["process"]["gc"]["tracked_objects"] is not None
    assert "tracked_objects" not in without_count["process"]["gc"]


def test_autostart_guard_blocks_only_below_the_floor():
    assert autostart_memory_block_reason(512.0, 700.0) is None
    reason = autostart_memory_block_reason(512.0, 84.0)
    assert reason is not None
    assert "84" in reason and "512" in reason


def test_autostart_guard_is_disabled_or_skipped():
    # floor <= 0 means the operator opted out of the guard entirely.
    assert autostart_memory_block_reason(0.0, 1.0) is None
    assert autostart_memory_block_reason(-5.0, 1.0) is None
    # No psutil reading must never block the feature.
    assert autostart_memory_block_reason(512.0, None) is None


def test_available_memory_mb_is_a_positive_number_or_none():
    value = available_memory_mb()
    assert value is None or value > 0

