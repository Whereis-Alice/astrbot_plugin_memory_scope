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
