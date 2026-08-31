"""Config parsing: every knob is clamped to a sane range."""

from __future__ import annotations

from core.collector import Settings
from core.tracemalloc_probe import DEFAULT_FRAMES


def test_defaults_from_empty_config():
    settings = Settings.from_config({})

    # Off by default: tracing costs memory on the very host being diagnosed.
    assert settings.auto_start_tracemalloc is False
    assert settings.auto_start_min_available_mb == 512.0
    assert settings.tracemalloc_frames == DEFAULT_FRAMES
    assert settings.sample_interval_seconds == 60
    assert settings.history_size == 720
    assert settings.deep_scan_enabled is True
    assert settings.deep_scan_max_objects == 120_000
    assert settings.deep_scan_max_objects_total == 400_000
    assert settings.deep_scan_time_budget_ms == 3000
    assert settings.include_object_count is False
    assert settings.alert_plugin_mb == 0.0
    assert settings.alert_growth_mb_per_hour == 0.0
    assert settings.persist_history is True
    assert settings.command_top_n == 8
    # Not user configurable, so it must keep its default no matter the config.
    assert settings.registry_ttl_seconds == 30.0


def test_config_without_get_falls_back_to_defaults():
    assert Settings.from_config(None).tracemalloc_frames == DEFAULT_FRAMES
    assert Settings.from_config(object()).sample_interval_seconds == 60
    assert Settings.from_config([]).history_size == 720


def test_values_are_read_from_the_config():
    settings = Settings.from_config(
        {
            "auto_start_tracemalloc": True,
            "auto_start_min_available_mb": 128,
            "tracemalloc_frames": 20,
            "sample_interval_seconds": 120,
            "history_size": 1000,
            "deep_scan_enabled": False,
            "deep_scan_max_objects": 50_000,
            "deep_scan_max_objects_total": 200_000,
            "deep_scan_time_budget_ms": 1500,
            "include_object_count": True,
            "alert_plugin_mb": 256.5,
            "alert_growth_mb_per_hour": 12.5,
            "persist_history": False,
            "command_top_n": 15,
            "registry_ttl_seconds": 999,
        },
    )

    assert settings.auto_start_tracemalloc is True
    assert settings.auto_start_min_available_mb == 128.0
    assert settings.tracemalloc_frames == 20
    assert settings.sample_interval_seconds == 120
    assert settings.history_size == 1000
    assert settings.deep_scan_enabled is False
    assert settings.deep_scan_max_objects == 50_000
    assert settings.deep_scan_max_objects_total == 200_000
    assert settings.deep_scan_time_budget_ms == 1500
    assert settings.include_object_count is True
    assert settings.alert_plugin_mb == 256.5
    assert settings.alert_growth_mb_per_hour == 12.5
    assert settings.persist_history is False
    assert settings.command_top_n == 15
    assert settings.registry_ttl_seconds == 30.0


def test_integers_are_clamped_at_both_ends():
    low = Settings.from_config(
        {
            "tracemalloc_frames": 0,
            "sample_interval_seconds": 1,
            "history_size": 1,
            "deep_scan_max_objects": 1,
            "deep_scan_max_objects_total": 1,
            "deep_scan_time_budget_ms": 1,
            "command_top_n": 0,
        },
    )
    assert low.tracemalloc_frames == 1
    assert low.sample_interval_seconds == 10
    assert low.history_size == 30
    assert low.deep_scan_max_objects == 5_000
    assert low.deep_scan_max_objects_total == 10_000
    assert low.deep_scan_time_budget_ms == 200
    assert low.command_top_n == 1

    high = Settings.from_config(
        {
            "tracemalloc_frames": 9_999,
            "sample_interval_seconds": 99_999,
            "history_size": 99_999,
            "deep_scan_max_objects": 99_999_999,
            "deep_scan_max_objects_total": 99_999_999,
            "deep_scan_time_budget_ms": 99_999_999,
            "command_top_n": 500,
        },
    )
    assert high.tracemalloc_frames == 40
    assert high.sample_interval_seconds == 3600
    assert high.history_size == 5000
    assert high.deep_scan_max_objects == 2_000_000
    assert high.deep_scan_max_objects_total == 8_000_000
    assert high.deep_scan_time_budget_ms == 60_000
    assert high.command_top_n == 30


def test_negative_alert_thresholds_become_zero():
    settings = Settings.from_config(
        {"alert_plugin_mb": -10, "alert_growth_mb_per_hour": -1.5},
    )

    assert settings.alert_plugin_mb == 0.0
    assert settings.alert_growth_mb_per_hour == 0.0


def test_none_values_fall_back_to_defaults():
    settings = Settings.from_config(
        {
            "tracemalloc_frames": None,
            "sample_interval_seconds": None,
            "alert_plugin_mb": None,
            "auto_start_tracemalloc": None,
            "auto_start_min_available_mb": None,
            "persist_history": None,
        },
    )

    assert settings.tracemalloc_frames == DEFAULT_FRAMES
    assert settings.sample_interval_seconds == 60
    assert settings.alert_plugin_mb == 0.0
    assert settings.auto_start_tracemalloc is False
    assert settings.persist_history is True


def test_unparsable_values_fall_back_to_defaults():
    settings = Settings.from_config(
        {
            "tracemalloc_frames": "not-a-number",
            "history_size": [1, 2, 3],
            "alert_plugin_mb": "soon",
            "command_top_n": {},
        },
    )

    assert settings.tracemalloc_frames == DEFAULT_FRAMES
    assert settings.history_size == 720
    assert settings.alert_plugin_mb == 0.0
    assert settings.command_top_n == 8


def test_numeric_strings_are_accepted():
    settings = Settings.from_config(
        {"tracemalloc_frames": "16", "alert_plugin_mb": "128.5"},
    )

    assert settings.tracemalloc_frames == 16
    assert settings.alert_plugin_mb == 128.5


def test_negative_memory_floor_becomes_zero_meaning_no_guard():
    settings = Settings.from_config({"auto_start_min_available_mb": -5})

    assert settings.auto_start_min_available_mb == 0.0
