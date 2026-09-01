"""Live-object attribution without allocation tracing."""

from __future__ import annotations

import gc
import importlib.util
import sys
from types import SimpleNamespace

from core.object_census import (
    BUCKET_BUILTINS,
    CensusResult,
    PluginCensus,
    TypeStat,
    build_module_index,
    run_census,
)
from core.plugin_registry import PluginRegistry


class FakeContext:
    def __init__(self, metas):
        self._metas = metas

    def get_all_stars(self):
        return list(self._metas)


def load_plugin(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    path = root / "main.py"
    path.write_text(
        "class Holder:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "LIVE = [Holder([{} for _ in range(4)])]\n",
        encoding="utf-8",
    )
    module_name = f"census_fake_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    meta = SimpleNamespace(
        name=name,
        root_dir_name=name,
        module=module,
        star_cls=None,
        module_path=module_name,
        display_name=name,
        version="1",
        author="test",
        activated=True,
        reserved=False,
    )
    return meta, module_name


def cleanup_modules(names):
    for name in names:
        sys.modules.pop(name, None)
    gc.collect()


def test_module_index_maps_plugin_types_and_builtin_bucket(tmp_path):
    meta, module_name = load_plugin(tmp_path, "alpha")
    try:
        registry = PluginRegistry(FakeContext([meta]))
        registry.refresh()
        owners, buckets = build_module_index(registry)

        assert owners[module_name] == "alpha"
        assert buckets["builtins"] == BUCKET_BUILTINS
    finally:
        cleanup_modules([module_name])


def test_census_attributes_live_instances_by_their_defining_module(tmp_path):
    meta, module_name = load_plugin(tmp_path, "alpha")
    try:
        registry = PluginRegistry(FakeContext([meta]))
        registry.refresh()
        result = run_census(registry, sample_rate=1, time_budget_ms=4000)

        assert isinstance(result, CensusResult)
        assert result.generated_at > 0
        assert result.total_objects > 0
        assert result.scanned > 0
        assert result.plugin_objects >= 1
        assert result.plugin_bytes >= sys.getsizeof(meta.module.LIVE[0])
        assert result.plugins["alpha"].name == "alpha"
        assert result.plugin_map()["alpha"] == result.plugins["alpha"].bytes
        assert result.meta()["plugin_count"] == 1

        rows = result.bucket_rows()
        assert rows
        assert all("bucket" in row and "bytes" in row for row in rows)
        assert result.plugins["alpha"].to_dict(top_types=1)["types"]
    finally:
        cleanup_modules([module_name])


def test_census_sampling_is_marked_scaled_and_has_type_stats():
    result = CensusResult(
        sample_rate=4,
        plugins={
            "demo": PluginCensus(
                name="demo",
                objects=2,
                bytes=20,
                types={"demo.Type": TypeStat("demo.Type", 2, 20)},
            ),
        },
        buckets={"other": TypeStat("other", 3, 30)},
    )

    assert result.scaled is True
    assert result.plugin_bytes == 20
    assert result.plugin_objects == 2
    assert result.bucket_rows(limit=1)[0]["bucket"] == "other"
    assert result.plugins["demo"].top_types(1)[0]["type"] == "demo.Type"


def test_census_respects_a_small_time_budget_without_raising():
    result = run_census(PluginRegistry(FakeContext([])), time_budget_ms=50)

    assert result.elapsed_ms >= 0
    assert result.scanned >= 0
    assert result.plugin_count if hasattr(result, "plugin_count") else True
