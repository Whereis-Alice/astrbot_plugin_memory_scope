"""Collector integration tests for the lightweight v2 probes."""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import sys
import types
from types import SimpleNamespace

import pytest

from core.collector import MemoryCollector, Settings
from core.import_cost import PackageCost, PluginImportCost, reset_ledger
from core.object_census import CensusResult, PluginCensus, TypeStat
from core.proc_memory import SmapsRollupReader
from core.sampler import HistoryStore

PLUGIN_ID = "astrbot_plugin_memory_scope"


class FakeContext:
    def __init__(self, metas):
        self._metas = list(metas)
        self.cached_config = {}

    def get_all_stars(self):
        return list(self._metas)


@pytest.fixture(autouse=True)
def clean_ledger():
    reset_ledger()
    yield
    reset_ledger()
    for name in list(sys.modules):
        if name.startswith("fake_"):
            sys.modules.pop(name, None)


def make_plugin(tmp_path, name, *, display_name=None, source="CACHE = []\n", payload_size=0):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    path = root / "main.py"
    path.write_text(source, encoding="utf-8")

    module_name = f"fake_{name}_module"
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    sys.modules[module_name] = module
    if payload_size:
        module.CACHE = [bytearray(payload_size)]
    plugin_type = type("FakePlugin", (), {"__module__": module_name})
    star = plugin_type()
    star.state = {"buffer": bytearray(payload_size or 1024)}
    meta = SimpleNamespace(
        name=display_name or name,
        root_dir_name=name,
        module=module,
        star_cls=star,
        module_path=f"data.plugins.{name}.main",
        display_name=display_name or name.replace("_", " ").title(),
        version="0.9.0",
        author="tester",
        activated=True,
        reserved=False,
    )
    return meta, module, star


def build_collector(metas, **config):
    return MemoryCollector(
        FakeContext(metas),
        Settings.from_config(config),
        PLUGIN_ID,
    )


def run(coro):
    return asyncio.run(coro)


def test_report_shape_is_rss_only_by_default(tmp_path):
    meta, _module, _star = make_plugin(tmp_path, "plugin_demo", payload_size=40_000)
    collector = build_collector(
        [meta],
        deep_scan_enabled=False,
        dep_audit_enabled=False,
    )

    report = run(collector.build_report(record_sample=True))

    assert set(report) >= {
        "generated_at",
        "process",
        "plugins",
        "packages",
        "census_buckets",
        "census_meta",
        "audit_meta",
        "opportunities",
        "totals",
        "deep_meta",
        "history",
        "notes",
        "self_plugin",
    }
    assert report["self_plugin"] == PLUGIN_ID
    assert report["process"]["pid"] > 0
    assert report["process"]["rss_bytes"] >= 0
    assert "gc" in report["process"]
    assert report["census_meta"] is None
    assert report["totals"]["census_bytes"] == 0
    assert report["history"]["samples"] == 1
    assert report["history"]["census_samples"] == 0
    assert "census_never_run" in report["notes"]
    assert "dep_audit_never_run" in report["notes"]

    row = report["plugins"][0]
    assert row["name"] == "plugin_demo"
    assert row["import_measured"] is False
    assert row["import_bytes"] is None
    assert row["census_bytes"] == 0
    assert row["retained"] is None


def test_directory_key_finds_import_cost_even_when_display_name_differs(tmp_path):
    meta, _module, _star = make_plugin(
        tmp_path,
        "astrbot_plugin_memory_scope",
        display_name="MemoryScope",
    )
    collector = build_collector([meta], deep_scan_enabled=False, dep_audit_enabled=False)
    collector.ledger.plugins["astrbot_plugin_memory_scope"] = PluginImportCost(
        name="astrbot_plugin_memory_scope",
        bytes=1234,
        self_bytes=777,
        modules=3,
    )

    report = run(collector.build_report(record_sample=False))
    row = report["plugins"][0]
    assert row["name"] == "MemoryScope"
    assert row["import_key"] == "astrbot_plugin_memory_scope"
    assert row["import_bytes"] == 1234
    assert row["import_self_bytes"] == 777
    assert row["is_self"] is True


def test_manual_census_is_reported_but_not_repeated_in_rss_only_history(tmp_path):
    meta, module, _star = make_plugin(tmp_path, "plugin_live", payload_size=80_000)
    collector = build_collector(
        [meta],
        deep_scan_enabled=False,
        dep_audit_enabled=False,
        census_sample_rate=1,
    )

    first = run(collector.census_now())
    assert first["census_meta"] is not None
    assert first["history"]["samples"] == 1
    assert first["history"]["census_samples"] == 1

    # The cached census remains visible in the report, but the next ordinary
    # tick is intentionally RSS-only rather than a duplicate stale snapshot.
    module.CACHE.append(bytearray(20_000))
    second = run(collector.build_report(record_sample=True))
    assert second["census_meta"] is not None
    assert second["history"]["samples"] == 2
    assert second["history"]["census_samples"] == 1
    assert collector.history.samples()[-1].has_attribution is False


def test_audit_is_cached_and_detail_contains_all_lightweight_layers(tmp_path):
    source = (
        "import heavy_pkg\n"
        "from another_pkg import Thing\n"
        "def lazy():\n"
        "    import lazy_pkg\n"
    )
    meta, _module, _star = make_plugin(tmp_path, "plugin_a", source=source)
    collector = build_collector([meta], deep_scan_enabled=False)
    collector.ledger.packages["heavy_pkg"] = PackageCost(
        name="heavy_pkg", bytes=10_000, self_bytes=10_000,
        wall_ms=1.0, modules=2, imports=1, first_importer="plugin_a",
    )
    collector.ledger.packages["another_pkg"] = PackageCost(
        name="another_pkg", bytes=2_000, self_bytes=2_000,
        wall_ms=1.0, modules=1, imports=1, first_importer="plugin_a",
    )
    collector.ledger.plugins["plugin_a"] = PluginImportCost(
        name="plugin_a", bytes=12_000, self_bytes=0, modules=3,
        packages=["heavy_pkg", "another_pkg"],
    )

    report = run(collector.build_report(detail_for="plugin_a", record_sample=False))
    assert report["audit_meta"]["finding_count"] == 2
    assert report["plugins"][0]["lazy_savings_bytes"] == 12_000
    assert report["detail"]["found"] is True
    assert report["detail"]["audit"]["known_bytes"] == 12_000
    assert {item["name"] for item in report["detail"]["import_packages"]} == {
        "heavy_pkg",
        "another_pkg",
    }

    # A second ordinary report reuses the AST result.
    again = run(collector.build_report(record_sample=False))
    assert again["audit_meta"]["generated_at"] == report["audit_meta"]["generated_at"]


def test_deep_scan_is_opt_in_per_request_and_reused(tmp_path):
    meta, module, star = make_plugin(tmp_path, "plugin_heavy", payload_size=120_000)
    collector = build_collector(
        [meta],
        deep_scan_enabled=True,
        dep_audit_enabled=False,
        deep_scan_time_budget_ms=5000,
    )
    # Keep a plugin-owned object reachable from both the module and instance.
    star.cache = module.CACHE

    report = run(collector.build_report(deep=True, record_sample=False))
    assert report["deep_meta"]["fresh"] is True
    assert report["deep_meta"]["generated_at"] is not None
    assert report["plugins"][0]["retained"] is not None
    assert report["plugins"][0]["retained"]["total_bytes"] > 0

    reused = run(collector.build_report(deep=False, record_sample=False))
    assert reused["deep_meta"]["fresh"] is False
    assert reused["plugins"][0]["retained"] == report["plugins"][0]["retained"]


def test_force_gc_returns_rss_measurements():
    collector = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)
    result = run(collector.force_gc())

    assert set(result) == {
        "collected",
        "rss_before",
        "rss_after",
        "freed_bytes",
        "uncollectable",
    }
    assert result["collected"] >= 0
    assert result["freed_bytes"] >= 0


def test_close_releases_the_process_rss_reader():
    collector = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)

    collector.close()

    assert collector.rss.closed is True


def test_apply_settings_rebuilds_history_ring_and_updates_alerts():
    collector = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)
    for index in range(40):
        collector.history.add(
            __import__("core.sampler", fromlist=["Sample"]).Sample(
                ts=float(index), rss_bytes=index,
            ),
        )
    collector.apply_settings(
        Settings.from_config(
            {
                "history_size": 10,
                "alert_plugin_mb": 64.0,
                "alert_growth_mb_per_hour": 8.0,
                "alert_rss_mb": 900,
            },
        ),
    )

    assert collector.history.max_samples == 30
    assert collector.history.count() == 30
    assert collector.history.samples()[0].ts == 10.0
    assert collector.alerts.size_mb == 64.0
    assert collector.alerts.growth_mb_per_hour == 8.0
    assert collector.alerts.rss_mb == 900.0


def test_census_result_can_be_injected_for_row_shape_regression(tmp_path):
    meta, _module, _star = make_plugin(tmp_path, "plugin_demo")
    collector = build_collector([meta], deep_scan_enabled=False, dep_audit_enabled=False)
    collector._census = CensusResult(
        plugins={
            "plugin_demo": PluginCensus(
                name="plugin_demo",
                objects=4,
                bytes=4096,
                types={"fake.Type": TypeStat("fake.Type", 4, 4096)},
            ),
        },
    )

    report = run(collector.build_report(record_sample=False))
    row = report["plugins"][0]
    assert row["census_bytes"] == 4096
    assert row["census_objects"] == 4
    assert row["census_types"][0]["type"] == "fake.Type"


REAL_ROLLUP = """Rss:              601268 kB
Pss:              598632 kB
Pss_Dirty:        476464 kB
Shared_Clean:       4228 kB
Private_Clean:    120576 kB
Private_Dirty:    476464 kB
Swap:             375160 kB
SwapPss:          375160 kB
"""


def fake_smaps(tmp_path, text=REAL_ROLLUP):
    """A SmapsRollupReader pointed at a file we control."""

    path = tmp_path / "smaps_rollup"
    path.write_text(text, encoding="utf-8")
    return SmapsRollupReader(min_interval_seconds=0.0, path=str(path))


def test_process_stats_report_the_footprint_not_just_rss(tmp_path):
    collector = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)
    collector.smaps = fake_smaps(tmp_path)

    stats = collector.process_stats()

    assert stats["pss_bytes"] == 598632 * 1024
    assert stats["swap_pss_bytes"] == 375160 * 1024
    assert stats["footprint_bytes"] == (598632 + 375160) * 1024
    assert stats["private_dirty_bytes"] == 476464 * 1024
    # The age marker travels with the numbers so the UI can admit staleness.
    assert stats["rollup_age_seconds"] >= 0.0
    # Exact and free; the one counter that sees a leak the allocator still hides.
    assert stats["allocated_blocks"] > 0
    assert stats["gc"]["allocated_blocks"] == stats["allocated_blocks"]


def test_totals_and_notes_follow_smaps_availability(tmp_path):
    collector = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)
    collector.smaps = fake_smaps(tmp_path)

    report = run(collector.build_report(record_sample=False))
    totals = report["totals"]
    assert totals["footprint_bytes"] == (598632 + 375160) * 1024
    assert totals["pss_bytes"] == 598632 * 1024
    assert totals["swap_pss_bytes"] == 375160 * 1024
    assert totals["private_dirty_bytes"] == 476464 * 1024
    assert totals["allocated_blocks"] > 0
    assert "smaps_unavailable" not in report["notes"]

    # A kernel without Pss support must say so instead of passing RSS off as a
    # footprint.
    blind = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)
    blind.smaps = SmapsRollupReader(path=str(tmp_path / "absent"))
    blind_report = run(blind.build_report(record_sample=False))
    assert blind_report["totals"]["footprint_bytes"] == 0
    assert "smaps_unavailable" in blind_report["notes"]


def test_rss_only_samples_still_carry_footprint_and_blocks(tmp_path):
    # v1.0.2 regression: when no probe ran, the sampler used to store nothing at
    # all, leaving the trend chart permanently empty on default settings.
    collector = build_collector([], deep_scan_enabled=False, dep_audit_enabled=False)
    collector.smaps = fake_smaps(tmp_path)

    run(collector.build_report(record_sample=True))

    latest = collector.history.samples()[-1]
    assert latest.has_attribution is False
    assert latest.has_retained is False
    assert latest.rss_bytes > 0
    assert latest.footprint_bytes == (598632 + 375160) * 1024
    assert latest.allocated_blocks > 0


def test_attribution_block_is_honest_about_coverage(tmp_path):
    meta, module, star = make_plugin(tmp_path, "plugin_fat", payload_size=200_000)
    star.cache = module.CACHE
    collector = build_collector(
        [meta],
        deep_scan_enabled=True,
        dep_audit_enabled=False,
        deep_scan_time_budget_ms=5000,
    )
    collector.smaps = fake_smaps(tmp_path)

    report = run(collector.build_report(deep=True, record_sample=True))
    attribution = report["attribution"]

    assert set(attribution) == {
        "method",
        "measured_bytes",
        "exclusive_bytes",
        "shared_bytes",
        "self_bytes",
        "private_dirty_bytes",
        "footprint_bytes",
        "coverage_percent",
        "plugin_count",
        "complete_count",
        "truncated_count",
        "scanned_objects",
        "generated_at",
        "elapsed_ms",
        "work_ms",
        "fresh",
    }
    assert attribution["method"] == "retained-graph"
    assert attribution["fresh"] is True
    assert attribution["measured_bytes"] > 0
    assert attribution["scanned_objects"] > 0
    assert attribution["plugin_count"] == 1
    # Coverage is measured against private dirty pages and can never reach 100%:
    # interpreter internals and C extension arenas are unreachable from Python.
    assert 0.0 < attribution["coverage_percent"] < 100.0

    assert report["totals"]["retained_bytes"] > 0
    assert (
        report["totals"]["retained_exclusive_bytes"]
        + report["totals"]["retained_shared_bytes"]
        == report["totals"]["retained_bytes"]
    )
    assert report["history"]["retained_samples"] == 1
    assert "retained_never_run" not in report["notes"]


def test_retained_scan_is_scheduled_not_run_on_every_tick():
    collector = build_collector([], deep_scan_enabled=True, deep_scan_interval_samples=5)

    # The first scan comes early so the dashboard is not blank for five
    # intervals after a restart.
    assert collector.deep_scan_due() is False
    collector._samples_since_deep = 2
    assert collector.deep_scan_due() is True

    # Afterwards it settles into the configured interval.
    collector._deep_rounds = 1
    collector._samples_since_deep = 4
    assert collector.deep_scan_due() is False
    collector._samples_since_deep = 5
    assert collector.deep_scan_due() is True


def test_retained_scan_can_be_switched_off_entirely():
    off = build_collector([], deep_scan_enabled=False, deep_scan_interval_samples=1)
    off._samples_since_deep = 99
    assert off.deep_scan_due() is False

    manual = build_collector(
        [],
        deep_scan_enabled=True,
        deep_scan_interval_samples=0,
    )
    manual._samples_since_deep = 99
    # 0 means manual-only: the button still works, the sampler never triggers it.
    assert manual.deep_scan_due() is False


def test_deep_request_is_ignored_when_the_scan_is_disabled(tmp_path):
    meta, _module, _star = make_plugin(tmp_path, "plugin_quiet")
    collector = build_collector([meta], deep_scan_enabled=False, dep_audit_enabled=False)

    report = run(collector.build_report(deep=True, record_sample=False))

    assert report["deep_meta"]["fresh"] is False
    assert report["deep_meta"]["rounds"] == 0
    assert report["attribution"]["measured_bytes"] == 0
    assert report["attribution"]["coverage_percent"] is None
    assert report["plugins"][0]["retained"] is None
