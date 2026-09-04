"""Human-readable command output tests."""

from __future__ import annotations

from core.text_report import (
    format_bytes,
    format_delta,
    format_ms,
    format_trend,
    render_audit,
    render_census,
    render_gc,
    render_imports,
    render_overview,
    render_plugin_detail,
)

MB = 1024 * 1024


def test_format_helpers():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1023) == "1023 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1536, precision=2) == "1.50 KB"
    assert format_bytes(5 * MB) == "5.0 MB"
    assert format_bytes(None) == "0 B"
    assert format_bytes("bad") == "-"
    assert format_bytes(-2048) == "-2.0 KB"
    assert format_delta(None) == ""
    assert format_delta(4096) == "+4.0 KB"
    assert format_delta(-4096) == "-4.0 KB"
    assert format_trend(MB) == "60.0 MB/h"
    assert format_trend(None) == ""
    assert format_ms(999) == "999 ms"
    assert format_ms(1500) == "1.50 s"


def report():
    return {
        "process": {
            "rss_bytes": 512 * MB,
            "memory_percent": 6.25,
            "threads": 12,
            "modules": 3000,
            "vms_bytes": 2 * 1024 * MB,
            "uptime_seconds": 3661,
            "pid": 123,
            "python_version": "3.12.0",
            "gc": {"counts": [1, 2, 3], "thresholds": [700, 10, 10], "collections": [4, 5, 6], "uncollectable": 0},
            "import_hook": {"installed": False, "overhead_ms": 1.2, "rss_growth_bytes": 200 * MB},
        },
        "plugins": [
            {
                "name": "plugin_big",
                "display_name": "Big Plugin",
                "import_measured": True,
                "import_bytes": 30 * MB,
                "import_self_bytes": 10 * MB,
                "import_ms": 1200,
                "import_modules": 20,
                "import_packages": ["numpy"],
                "census_bytes": 8 * MB,
                "census_objects": 100,
                "census_types": [{"type": "plugin_big.Cache", "bytes": 4 * MB, "objects": 10}],
                "delta_bytes": 2 * MB,
                "trend_bytes_per_minute": MB,
                "is_self": False,
                "retained": {"exclusive_bytes": 4 * MB, "shared_bytes": MB, "exclusive_objects": 40},
                "version": "1.0",
                "author": "A",
                "module_path": "data.plugins.plugin_big.main",
                "root_dir": "/plugins/plugin_big",
            },
            {
                "name": "plugin_small",
                "display_name": "Small Plugin",
                "import_measured": False,
                "import_bytes": None,
                "import_self_bytes": None,
                "import_ms": None,
                "import_modules": None,
                "import_packages": [],
                "census_bytes": 0,
                "census_objects": 0,
                "census_types": [],
                "delta_bytes": None,
                "trend_bytes_per_minute": None,
                "is_self": False,
                "retained": None,
                "version": "",
                "author": "",
                "module_path": None,
                "root_dir": None,
            },
        ],
        "packages": [{"name": "numpy", "bytes": 20 * MB, "wall_ms": 500, "modules": 10, "first_importer": "plugin_big"}],
        "census_buckets": [{"bucket": "astrbot_core", "bytes": 100 * MB, "objects": 1000}],
        "census_meta": {"scanned": 10000, "total_objects": 10000, "sample_rate": 10, "elapsed_ms": 50, "plugin_bytes": 8 * MB, "plugin_objects": 100, "plugin_count": 1},
        "audit_meta": {"audited": 2, "plugin_count": 2, "finding_count": 1, "elapsed_ms": 10},
        "opportunities": [{"module": "numpy", "cost_bytes": 20 * MB, "plugins": ["plugin_big"]}],
        "totals": {"plugin_count": 2, "measured_plugin_count": 1, "import_self_bytes_total": 10 * MB, "packages_bytes": 20 * MB, "packages_count": 1, "import_total_bytes": 30 * MB, "lazy_savings_bytes": 20 * MB, "census_bytes": 8 * MB, "census_objects": 100, "rss_bytes": 512 * MB},
        "deep_meta": {"generated_at": 1000, "elapsed_ms": 200, "fresh": True, "truncated": False},
        "history": {"samples": 3, "census_samples": 1, "interval_seconds": 60, "baseline_at": None},
        "notes": ["census_sampled"],
    }


def test_render_overview_mentions_rss_import_and_census():
    text = render_overview(report(), top_n=2)

    assert "MemoryScope" in text
    assert "512.0 MB" in text
    assert "插件代码" in text
    assert "第三方包" in text
    assert "改惰性导入可省" in text
    assert "对象普查" in text
    assert "Big Plugin" in text
    assert "趋势" in text

def test_render_overview_leads_with_footprint_when_smaps_available():
    data = report()
    data["process"]["footprint_bytes"] = 973 * MB
    data["process"]["pss_bytes"] = 598 * MB
    data["process"]["swap_pss_bytes"] = 375 * MB
    data["totals"]["retained_bytes"] = 5 * MB
    data["totals"]["retained_exclusive_bytes"] = 4 * MB
    data["totals"]["retained_shared_bytes"] = MB
    data["attribution"] = {
        "method": "retained-graph",
        "measured_bytes": 5 * MB,
        "private_dirty_bytes": 100 * MB,
        "coverage_percent": 5.0,
    }

    text = render_overview(data, top_n=2)
    head = text.splitlines()[1]

    assert "真实足迹" in head
    assert "973.0 MB" in head
    # RSS stays visible next to it so the paged-out gap is not hidden.
    assert "RSS 512.0 MB" in head
    assert "已换出 375.0 MB" in head
    assert "引用图归因" in text
    assert "独占 4.0 MB" in text
    assert "共享均摊 1.0 MB" in text
    assert "覆盖 Private_Dirty 的 5.0%" in text


def test_render_overview_falls_back_to_rss_without_smaps():
    data = report()
    data["notes"] = ["smaps_unavailable", "retained_never_run"]

    text = render_overview(data, top_n=2)
    head = text.splitlines()[1]

    # No Pss means no footprint claim at all - never silently reuse RSS.
    assert head.startswith("进程 RSS 512.0 MB")
    assert "真实足迹" not in text
    assert "内核未提供 smaps_rollup" in text
    assert "引用图归因尚未跑过第一轮" in text


def test_render_imports_and_audit_are_explicit_about_shared_costs():
    imports = render_imports(report(), top_n=5)
    assert "numpy" in imports
    assert "首个导入者" in imports
    assert "搭便车" in imports

    audit = render_audit(report(), top_n=5)
    assert "numpy" in audit
    assert "只有列出的插件全部改成函数内导入" in audit


def test_render_census_and_detail_include_limits_and_layers():
    census = render_census(report(), top_n=2)
    assert "抽样 1/10" in census
    assert "str/bytes/int" in census

    detail = render_plugin_detail({"found": True, "name": "plugin_big", "row": report()["plugins"][0], "import": {"bytes": 30 * MB, "self_bytes": 10 * MB, "wall_ms": 1200, "modules": 20}, "import_packages": report()["packages"], "audit": {"known_bytes": 20 * MB, "imports": [{"module": "numpy", "cost_bytes": 20 * MB, "file": "main.py", "lineno": 1, "guarded": False}]}, "census": {"bytes": 8 * MB, "objects": 100, "type_count": 1, "types": report()["plugins"][0]["census_types"]}, "series": []})
    assert "加载成本" in detail
    assert "引用图保留" in detail
    assert "顶层重依赖" in detail
    assert "对象类型 Top" in detail


def test_render_empty_and_gc():
    assert "尚未运行" in render_census({})
    assert "未找到" in render_plugin_detail({"found": False})
    text = render_gc({"collected": 4, "rss_before": 100 * MB, "rss_after": 99 * MB, "freed_bytes": MB, "uncollectable": 0})
    assert "回收对象 4" in text
    assert "RSS 释放" in text
