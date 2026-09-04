"""Config parsing: every knob is clamped to a sane range."""

from __future__ import annotations

from core.collector import Settings
from core.dep_audit import DEFAULT_MAX_FILES, DEFAULT_TIME_BUDGET_MS
from core.import_cost import DEFAULT_MAX_OVERHEAD_MS


def test_defaults_from_empty_config():
    settings = Settings.from_config({})

    # The import hook is on by default: it is the only probe cheap enough to
    # run unconditionally (~2 us per first-time import, no allocation tracing).
    assert settings.measure_import_cost is True
    assert settings.import_hook_max_overhead_ms == DEFAULT_MAX_OVERHEAD_MS
    # The census walks the whole GC heap, so it must be opt-in.
    assert settings.census_enabled is False
    assert settings.census_sample_rate == 10
    assert settings.census_time_budget_ms == 4000
    assert settings.dep_audit_enabled is True
    assert settings.dep_audit_max_files == DEFAULT_MAX_FILES
    assert settings.dep_audit_time_budget_ms == DEFAULT_TIME_BUDGET_MS
    assert settings.sample_interval_seconds == 60
    assert settings.history_size == 720
    assert settings.deep_scan_enabled is True
    assert settings.deep_scan_max_objects == 120_000
    assert settings.deep_scan_max_objects_total == 400_000
    assert settings.deep_scan_time_budget_ms == 3000
    # The scan is spread over every 5th tick and yields the GIL every 15 ms at a
    # 25% duty cycle.  These three together are what keep it off the hot path.
    assert settings.deep_scan_interval_samples == 5
    assert settings.deep_scan_slice_ms == 15
    assert settings.deep_scan_duty_percent == 25
    # smaps_rollup is on by default but rate-limited: 5.4 ms per read is fine
    # once every 30 s, and fatal on every request.
    assert settings.proc_smaps_enabled is True
    assert settings.proc_smaps_min_interval_seconds == 30.0
    assert settings.include_object_count is False
    assert settings.alert_plugin_mb == 0.0
    assert settings.alert_growth_mb_per_hour == 0.0
    assert settings.alert_rss_mb == 0.0
    assert settings.alert_rss_growth_mb_per_hour == 0.0
    assert settings.persist_history is True
    assert settings.command_top_n == 8
    assert settings.registry_ttl_seconds == 30.0


def test_config_without_get_falls_back_to_defaults():
    assert Settings.from_config(None).measure_import_cost is True
    assert Settings.from_config(object()).sample_interval_seconds == 60
    assert Settings.from_config([]).history_size == 720


def test_values_are_read_from_the_config():
    settings = Settings.from_config(
        {
            "measure_import_cost": False,
            "import_hook_max_overhead_ms": 250,
            "census_enabled": True,
            "census_sample_rate": 1,
            "census_time_budget_ms": 9000,
            "dep_audit_enabled": False,
            "dep_audit_max_files": 1200,
            "dep_audit_time_budget_ms": 5000,
            "sample_interval_seconds": 120,
            "history_size": 1000,
            "deep_scan_enabled": False,
            "deep_scan_max_objects": 50_000,
            "deep_scan_max_objects_total": 200_000,
            "deep_scan_time_budget_ms": 1500,
            "deep_scan_interval_samples": 20,
            "deep_scan_slice_ms": 40,
            "deep_scan_duty_percent": 60,
            "proc_smaps_enabled": False,
            "proc_smaps_min_interval_seconds": 5,
            "include_object_count": True,
            "alert_plugin_mb": 256.5,
            "alert_growth_mb_per_hour": 12.5,
            "alert_rss_mb": 900,
            "alert_rss_growth_mb_per_hour": 40,
            "persist_history": False,
            "command_top_n": 15,
            "registry_ttl_seconds": 999,
        },
    )

    assert settings.measure_import_cost is False
    assert settings.import_hook_max_overhead_ms == 250.0
    assert settings.census_enabled is True
    assert settings.census_sample_rate == 1
    assert settings.census_time_budget_ms == 9000
    assert settings.dep_audit_enabled is False
    assert settings.dep_audit_max_files == 1200
    assert settings.dep_audit_time_budget_ms == 5000
    assert settings.sample_interval_seconds == 120
    assert settings.history_size == 1000
    assert settings.deep_scan_enabled is False
    assert settings.deep_scan_max_objects == 50_000
    assert settings.deep_scan_max_objects_total == 200_000
    assert settings.deep_scan_time_budget_ms == 1500
    assert settings.deep_scan_interval_samples == 20
    assert settings.deep_scan_slice_ms == 40
    assert settings.deep_scan_duty_percent == 60
    assert settings.proc_smaps_enabled is False
    assert settings.proc_smaps_min_interval_seconds == 5.0
    assert settings.include_object_count is True
    assert settings.alert_plugin_mb == 256.5
    assert settings.alert_growth_mb_per_hour == 12.5
    assert settings.alert_rss_mb == 900.0
    assert settings.alert_rss_growth_mb_per_hour == 40.0
    assert settings.persist_history is False
    assert settings.command_top_n == 15
    # registry_ttl_seconds is read too, it just has no schema entry.
    assert settings.registry_ttl_seconds == 999.0


def test_integers_are_clamped_at_both_ends():
    low = Settings.from_config(
        {
            "census_sample_rate": 0,
            "census_time_budget_ms": 1,
            "dep_audit_max_files": 0,
            "dep_audit_time_budget_ms": 1,
            "sample_interval_seconds": 1,
            "history_size": 1,
            "deep_scan_max_objects": 1,
            "deep_scan_max_objects_total": 1,
            "deep_scan_time_budget_ms": 1,
            "deep_scan_interval_samples": -5,
            "deep_scan_slice_ms": 0,
            "deep_scan_duty_percent": 0,
            "command_top_n": 0,
        },
    )
    assert low.census_sample_rate == 1
    assert low.census_time_budget_ms == 200
    assert low.dep_audit_max_files == 10
    assert low.dep_audit_time_budget_ms == 200
    assert low.sample_interval_seconds == 10
    assert low.history_size == 30
    assert low.deep_scan_max_objects == 5_000
    assert low.deep_scan_max_objects_total == 10_000
    assert low.deep_scan_time_budget_ms == 200
    # 0 is a legal value here: it means manual-only, so it is not clamped up.
    assert low.deep_scan_interval_samples == 0
    assert low.deep_scan_slice_ms == 1
    # A duty cycle below 5% would make the scan take minutes to finish.
    assert low.deep_scan_duty_percent == 5
    assert low.command_top_n == 1

    high = Settings.from_config(
        {
            "census_sample_rate": 99_999,
            "census_time_budget_ms": 99_999_999,
            "dep_audit_max_files": 99_999,
            "dep_audit_time_budget_ms": 99_999_999,
            "sample_interval_seconds": 99_999,
            "history_size": 99_999,
            "deep_scan_max_objects": 99_999_999,
            "deep_scan_max_objects_total": 99_999_999,
            "deep_scan_time_budget_ms": 99_999_999,
            "deep_scan_interval_samples": 99_999,
            "deep_scan_slice_ms": 99_999,
            "deep_scan_duty_percent": 99_999,
            "command_top_n": 500,
        },
    )
    assert high.census_sample_rate == 1000
    assert high.census_time_budget_ms == 60_000
    assert high.dep_audit_max_files == 5000
    assert high.dep_audit_time_budget_ms == 60_000
    assert high.sample_interval_seconds == 3600
    assert high.history_size == 5000
    assert high.deep_scan_max_objects == 2_000_000
    assert high.deep_scan_max_objects_total == 8_000_000
    assert high.deep_scan_time_budget_ms == 60_000
    assert high.deep_scan_interval_samples == 1000
    assert high.deep_scan_slice_ms == 200
    # 100% duty means "no yielding", which is the documented upper bound.
    assert high.deep_scan_duty_percent == 100
    assert high.command_top_n == 30


def test_negative_alert_thresholds_become_zero():
    settings = Settings.from_config(
        {
            "alert_plugin_mb": -10,
            "alert_growth_mb_per_hour": -1.5,
            "alert_rss_mb": -1,
            "alert_rss_growth_mb_per_hour": -2,
        },
    )

    assert settings.alert_plugin_mb == 0.0
    assert settings.alert_growth_mb_per_hour == 0.0
    assert settings.alert_rss_mb == 0.0
    assert settings.alert_rss_growth_mb_per_hour == 0.0


def test_none_values_fall_back_to_defaults():
    settings = Settings.from_config(
        {
            "measure_import_cost": None,
            "census_sample_rate": None,
            "sample_interval_seconds": None,
            "alert_plugin_mb": None,
            "persist_history": None,
        },
    )

    assert settings.measure_import_cost is True
    assert settings.census_sample_rate == 10
    assert settings.sample_interval_seconds == 60
    assert settings.alert_plugin_mb == 0.0
    assert settings.persist_history is True


def test_unparsable_values_fall_back_to_defaults():
    settings = Settings.from_config(
        {
            "census_sample_rate": "not-a-number",
            "history_size": [1, 2, 3],
            "alert_plugin_mb": "soon",
            "command_top_n": {},
            "import_hook_max_overhead_ms": "later",
        },
    )

    assert settings.census_sample_rate == 10
    assert settings.history_size == 720
    assert settings.alert_plugin_mb == 0.0
    assert settings.command_top_n == 8
    assert settings.import_hook_max_overhead_ms == DEFAULT_MAX_OVERHEAD_MS


def test_numeric_strings_are_accepted():
    settings = Settings.from_config(
        {"census_sample_rate": "16", "alert_plugin_mb": "128.5"},
    )

    assert settings.census_sample_rate == 16
    assert settings.alert_plugin_mb == 128.5


def test_zero_overhead_budget_means_unlimited():
    settings = Settings.from_config({"import_hook_max_overhead_ms": 0})

    assert settings.import_hook_max_overhead_ms == 0.0
