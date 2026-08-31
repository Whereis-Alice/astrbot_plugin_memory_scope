"""Path -> plugin resolution and non-plugin bucketing."""

from __future__ import annotations

import os
from types import SimpleNamespace

from core import plugin_registry
from core.plugin_registry import (
    BUCKET_CORE,
    BUCKET_OTHER,
    BUCKET_STDLIB,
    BUCKET_UNKNOWN,
    PluginRegistry,
)


class FakeContext:
    def __init__(self, metas):
        self._metas = metas

    def get_all_stars(self):
        return list(self._metas)


def make_meta(tmp_path, name, *, activated=True, reserved=False, module_file="main.py"):
    plugin_root = tmp_path / name
    plugin_root.mkdir(parents=True, exist_ok=True)
    entry_file = plugin_root / module_file
    entry_file.write_text("# plugin entry\n", encoding="utf-8")
    module = SimpleNamespace(**{"__file__": str(entry_file)})
    return SimpleNamespace(
        name=name,
        root_dir_name=name,
        module=module,
        star_cls=None,
        module_path="data.plugins." + name + ".main",
        display_name=name.replace("_", " ").title(),
        version="1.2.3",
        author="tester",
        activated=activated,
        reserved=reserved,
    )


def build_registry(tmp_path, metas):
    registry = PluginRegistry(FakeContext(metas))
    registry.refresh()
    return registry


def test_refresh_collects_entries(tmp_path):
    registry = build_registry(
        tmp_path,
        [make_meta(tmp_path, "plugin_alpha"), make_meta(tmp_path, "plugin_beta")],
    )

    assert {entry.name for entry in registry.entries} == {"plugin_alpha", "plugin_beta"}

    alpha = registry.get("plugin_alpha")
    assert alpha is not None
    assert alpha.root_dir == plugin_registry.normalize(str(tmp_path / "plugin_alpha"))
    assert alpha.has_instance is False

    payload = alpha.to_dict()
    assert payload["version"] == "1.2.3"
    assert payload["author"] == "tester"
    assert payload["activated"] is True
    assert payload["has_instance"] is False


def test_missing_context_hook_is_tolerated():
    registry = PluginRegistry(object())
    registry.refresh()
    assert registry.entries == []
    assert registry.get("anything") is None


def test_activated_entry_wins_over_duplicate(tmp_path):
    active = make_meta(tmp_path, "plugin_dup")
    inactive = make_meta(tmp_path, "plugin_dup", activated=False)
    registry = build_registry(tmp_path, [active, inactive])

    entry = registry.get("plugin_dup")
    assert entry is not None
    assert entry.activated is True


def test_resolve_path_matches_files_inside_plugin_root(tmp_path):
    registry = build_registry(tmp_path, [make_meta(tmp_path, "plugin_alpha")])
    inside = tmp_path / "plugin_alpha" / "sub" / "helpers.py"

    assert registry.resolve_path(str(inside)) == "plugin_alpha"
    assert registry.resolve_path(str(tmp_path / "elsewhere" / "mod.py")) is None
    assert registry.resolve_path("<frozen importlib._bootstrap>") is None
    assert registry.resolve_path(None) is None
    assert registry.resolve_path("") is None


def test_resolve_path_prefers_the_deepest_plugin_root(tmp_path):
    outer = make_meta(tmp_path, "plugin_outer")
    nested_dir = tmp_path / "plugin_outer" / "vendor"
    nested_dir.mkdir(parents=True, exist_ok=True)
    inner_file = nested_dir / "plugin_inner.py"
    inner_file.write_text("# nested\n", encoding="utf-8")
    inner = SimpleNamespace(
        name="plugin_inner",
        root_dir_name="vendor",
        module=SimpleNamespace(**{"__file__": str(inner_file)}),
        star_cls=None,
        module_path="vendor.plugin_inner",
        display_name="Inner",
        version="0.1.0",
        author="tester",
        activated=True,
        reserved=False,
    )

    registry = build_registry(tmp_path, [outer, inner])

    assert registry.resolve_path(str(nested_dir / "deep.py")) == "plugin_inner"
    assert registry.resolve_path(str(tmp_path / "plugin_outer" / "main.py")) == "plugin_outer"


def test_resolve_path_is_cached(tmp_path):
    registry = build_registry(tmp_path, [make_meta(tmp_path, "plugin_alpha")])
    target = str(tmp_path / "plugin_alpha" / "main.py")

    assert registry.resolve_path(target) == "plugin_alpha"
    # The second call must be served from the cache, even without the index.
    registry._dir_index = []
    assert registry.resolve_path(target) == "plugin_alpha"


def test_classify_path_buckets(tmp_path):
    registry = PluginRegistry(FakeContext([]))
    core_root = tmp_path / "astrbot"
    site = tmp_path / "site-packages"
    stdlib = tmp_path / "lib" / "python"
    for directory in (core_root, site, stdlib):
        directory.mkdir(parents=True, exist_ok=True)

    registry._core_root = plugin_registry._as_dir_key(str(core_root))
    registry._lib_roots = [
        (plugin_registry._as_dir_key(str(site)), plugin_registry.LIB_PREFIX),
    ]
    registry._stdlib_roots = [plugin_registry._as_dir_key(str(stdlib))]

    assert registry.classify_path(str(core_root / "core" / "star.py")) == BUCKET_CORE
    assert registry.classify_path(str(site / "httpx" / "_client.py")) == "lib:httpx"
    assert registry.classify_path(str(site / "six.py")) == "lib:six"
    assert registry.classify_path(str(stdlib / "json" / "decoder.py")) == BUCKET_STDLIB
    assert registry.classify_path(str(tmp_path / "random" / "file.py")) == BUCKET_OTHER
    assert registry.classify_path("<unknown>") == BUCKET_UNKNOWN
    assert registry.classify_path(None) == BUCKET_UNKNOWN


def test_classify_path_core_wins_over_site_packages(tmp_path):
    """AstrBot installed into site-packages must still be reported as core."""

    registry = PluginRegistry(FakeContext([]))
    site = tmp_path / "site-packages"
    core_root = site / "astrbot"
    core_root.mkdir(parents=True, exist_ok=True)

    registry._core_root = plugin_registry._as_dir_key(str(core_root))
    registry._lib_roots = [
        (plugin_registry._as_dir_key(str(site)), plugin_registry.LIB_PREFIX),
    ]
    registry._stdlib_roots = []

    assert registry.classify_path(str(core_root / "api" / "star.py")) == BUCKET_CORE


def test_normalize_is_absolute_and_case_insensitive_on_windows():
    assert os.path.isabs(plugin_registry.normalize("some/relative/path.py"))
    if os.name == "nt":
        left = plugin_registry.normalize("C:/Temp/A.PY")
        right = plugin_registry.normalize("c:\\temp\\a.py")
        assert left == right
