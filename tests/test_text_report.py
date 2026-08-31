"""Chat command renderers: formatting helpers and full reports."""

from __future__ import annotations

from core.text_report import (
    format_bytes,
    format_delta,
    format_trend,
    render_gc,
    render_overview,
    render_plugin_detail,
)

MB = 1024 * 1024


def test_format_bytes_units():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1023) == "1023 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(1536, precision=2) == "1.50 KB"
    assert format_bytes(5 * MB) == "5.0 MB"
    assert format_bytes(3 * 1024 * MB) == "3.0 GB"
    # The unit table stops at TB instead of overflowing.
    assert format_bytes(1024 ** 5) == "1024.0 TB"


def test_format_bytes_edge_cases():
    assert format_bytes(None) == "0 B"
    assert format_bytes(-2048) == "-2.0 KB"
    assert format_bytes("not-a-number") == "-"
    assert format_bytes(object()) == "-"


def test_format_delta():
    assert format_delta(None) == ""
    assert format_delta(0) == ""
    assert format_delta(512) == ""
    assert format_delta(4096) == "+4.0 KB"
    assert format_delta(-4096) == "-4.0 KB"
    assert format_delta("nope") == ""


def test_format_trend_converts_to_per_hour():
    assert format_trend(None) == ""
    assert format_trend(1024) == ""
    assert format_trend(MB) == "60.0 MB/h"
    assert format_trend(-MB) == "-60.0 MB/h"
    assert format_trend("nope") == ""


def make_report(**overrides):
    report = {
        "process": {
            "rss_bytes": 512 * MB,
            "memory_percent": 6.25,
            "tracemalloc": {
                "tracing": True,
                "current_bytes": 90 * MB,
                "peak_bytes": 120 * MB,
                "frames": 12,
            },
        },
        "totals": {
            "traced_bytes": 90 * MB,
            "plugin_bytes": 40 * MB,
            "plugin_count": 12,
            "measured_plugin_count": 3,
        },
        "plugins": [
            {
                "name": "plugin_a",
                "display_name": "Plugin A",
                "attributed_bytes": 30 * MB,
                "direct_bytes": 20 * MB,
                "blocks": 1200,
                "delta_bytes": 5 * MB,
                "trend_bytes_per_minute": MB,
                "retained": {"exclusive_bytes": 12 * MB, "shared_bytes": MB},
                "version": "1.0.0",
                "author": "tester",
            },
            {
                "name": "plugin_b",
                "display_name": "Plugin B",
                "attributed_bytes": 8 * MB,
                "direct_bytes": 8 * MB,
                "blocks": 300,
                "delta_bytes": None,
                "trend_bytes_per_minute": None,
                "retained": None,
            },
            {
                "name": "plugin_c",
                "display_name": "Plugin C",
                "attributed_bytes": 2 * MB,
                "direct_bytes": MB,
                "blocks": 100,
                "delta_bytes": None,
                "trend_bytes_per_minute": None,
                "retained": None,
            },
            {
                "name": "plugin_idle",
                "display_name": "Plugin Idle",
                "attributed_bytes": 0,
                "direct_bytes": 0,
                "blocks": 0,
                "delta_bytes": None,
                "trend_bytes_per_minute": None,
                "retained": None,
            },
        ],
        "others": [
            {"bucket": "astrbot_core", "bytes": 30 * MB},
            {"bucket": "lib:httpx", "bytes": 10 * MB},
            {"bucket": "python_stdlib", "bytes": 5 * MB},
        ],
        "notes": [],
    }
    report.update(overrides)
    return report


def test_render_overview_happy_path():
    text = render_overview(make_report())

    assert "MemoryScope" in text
    assert "512.0 MB" in text
    assert "6.2%" in text or "6.3%" in text
    assert "峰值 120.0 MB" in text
    assert "1. Plugin A 30.0 MB" in text
    assert "Δ+5.0 MB" in text
    assert "趋势 60.0 MB/h" in text
    assert "独占 12.0 MB" in text
    assert "2. Plugin B 8.0 MB" in text
    # Plugins without attributed bytes are not listed at all.
    assert "Plugin Idle" not in text
    assert "非插件部分合计 45.0 MB" in text
    assert "astrbot_core 30.0 MB" in text


def test_render_overview_respects_top_n_and_reports_the_rest():
    text = render_overview(make_report(), top_n=2)

    assert "1. Plugin A" in text
    assert "2. Plugin B" in text
    assert "Plugin C" not in text
    assert "其余 1 个插件占用较小" in text


def test_render_overview_without_tracing():
    report = make_report(notes=["tracemalloc_off"])
    report["process"]["tracemalloc"]["tracing"] = False

    text = render_overview(report)

    assert "tracemalloc 未开启" in text
    assert "/mem trace on" in text


def test_render_overview_warns_about_late_tracing_and_missing_psutil():
    text = render_overview(
        make_report(notes=["tracing_started_late", "psutil_missing"]),
    )

    assert "PYTHONTRACEMALLOC" in text
    assert "未安装 psutil" in text


def test_render_overview_without_data():
    text = render_overview(
        {
            "process": {"rss_bytes": 0, "tracemalloc": {"tracing": False}},
            "totals": {},
            "plugins": [],
            "notes": [],
        },
    )

    assert "暂无可归因数据" in text


def test_render_overview_tolerates_an_empty_payload():
    assert render_overview({}).splitlines()[0].endswith("内存占用")


def test_render_plugin_detail_not_found():
    assert "未找到该插件" in render_plugin_detail({"found": False})
    assert "未找到该插件" in render_plugin_detail({})


def test_render_plugin_detail_full():
    row = make_report()["plugins"][0]
    row["retained"] = {
        "exclusive_bytes": 12 * MB,
        "shared_bytes": MB,
        "exclusive_objects": 4200,
        "truncated": True,
    }
    detail = {
        "name": "plugin_a",
        "found": True,
        "row": row,
        "lines": [
            {
                "filename": "/data/plugins/plugin_a/core/cache.py",
                "lineno": 88,
                "bytes": 9 * MB,
                "blocks": 300,
            },
            {
                "filename": "single.py",
                "lineno": 1,
                "bytes": MB,
                "blocks": 10,
            },
        ],
    }

    text = render_plugin_detail(detail)

    assert "Plugin A" in text
    assert "版本 1.0.0 · 作者 tester" in text
    assert "归因分配 30.0 MB" in text
    assert "其中直接分配 20.0 MB" in text
    assert "独占 12.0 MB" in text
    assert "（已截断）" in text
    assert "相对基线 +5.0 MB" in text
    assert "分配热点：" in text
    assert "core/cache.py:88 9.0 MB (300 块)" in text
    assert "single.py:1" in text


def test_render_plugin_detail_limits_hotspots():
    detail = {
        "name": "plugin_a",
        "found": True,
        "row": {"display_name": "Plugin A", "attributed_bytes": MB},
        "lines": [
            {"filename": "a.py", "lineno": index, "bytes": MB, "blocks": 1}
            for index in range(6)
        ],
    }

    text = render_plugin_detail(detail, limit=2)

    assert text.count("a.py:") == 2


def test_render_plugin_detail_minimal_row():
    text = render_plugin_detail({"name": "plugin_x", "found": True, "row": {}})

    assert "plugin_x" in text
    assert "归因分配 0 B" in text


def test_render_gc():
    text = render_gc(
        {
            "collected": 128,
            "traced_before": 100 * MB,
            "traced_after": 90 * MB,
            "freed_bytes": 10 * MB,
            "uncollectable": 2,
        },
    )

    assert "回收对象 128 个" in text
    assert "100.0 MB" in text
    assert "90.0 MB" in text
    assert "释放 10.0 MB" in text
    assert "不可回收对象 2 个" in text
