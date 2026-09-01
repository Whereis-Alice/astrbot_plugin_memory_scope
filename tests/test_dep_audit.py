"""Static top-level dependency audit tests."""

from __future__ import annotations

from types import SimpleNamespace

from core.dep_audit import (
    DependencyAuditor,
    PluginAudit,
    RawImport,
    collect_module_level_imports,
)


def test_ast_walker_keeps_only_imports_that_run_at_module_level():
    import ast

    source = """
import numpy
from pandas.core import frame
from . import local
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import never_runtime

try:
    import optional_dep
except ImportError:
    pass

if enabled:
    import conditional_dep

def function():
    import lazy_dep

class Thing:
    import class_dep

for item in values:
    import loop_dep

match value:
    case _:
        import match_dep
"""

    found = collect_module_level_imports(ast.parse(source), "main.py")

    assert [(item.top, item.guarded) for item in found] == [
        ("numpy", False),
        ("pandas", False),
        ("typing", False),
        ("optional_dep", True),
        ("conditional_dep", False),
        ("class_dep", False),
        ("loop_dep", False),
        ("match_dep", False),
    ]


def make_plugin(root, name, source):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "main.py").write_text(source, encoding="utf-8")
    return SimpleNamespace(name=name, root_dir=str(directory))


def test_audit_matches_costs_deduplicates_imports_and_finds_shared_opportunities(tmp_path):
    alpha = make_plugin(
        tmp_path,
        "alpha_dir",
        """
import heavy_a
import heavy_a
from heavy_b import Thing
from stdlib_only import x
from .local import y
""",
    )
    beta = make_plugin(
        tmp_path,
        "beta_dir",
        """
import heavy_a
try:
    import heavy_c
except ModuleNotFoundError:
    pass
""",
    )
    missing = SimpleNamespace(name="missing", root_dir=None)

    result = DependencyAuditor(max_files=20, time_budget_ms=2000).run(
        [alpha, beta, missing],
        {"heavy_a": 1000, "heavy_b": 200, "heavy_c": 50},
    )

    alpha_audit = result["audits"]["alpha_dir"]
    assert isinstance(alpha_audit, PluginAudit)
    assert alpha_audit.files == 1
    assert [item.module for item in alpha_audit.imports] == [
        "heavy_a",
        "heavy_b",
        "stdlib_only",
    ]
    assert alpha_audit.known_bytes == 1200
    assert alpha_audit.unknown_modules == ["stdlib_only"]
    assert result["audits"]["missing"].error == "no_source_dir"

    opportunities = {item["module"]: item for item in result["opportunities"]}
    assert opportunities["heavy_a"]["cost_bytes"] == 1000
    assert opportunities["heavy_a"]["plugins"] == ["alpha_dir", "beta_dir"]
    assert opportunities["heavy_a"]["shared_by"] == 2
    assert opportunities["heavy_c"]["guarded"] == 1


def test_audit_cache_reuses_source_scan_until_file_signature_changes(tmp_path):
    plugin = make_plugin(tmp_path, "cached", "import heavy\n")
    auditor = DependencyAuditor()

    first = auditor.run([plugin], {"heavy": 10})
    cache_size = len(auditor._cache)
    second = auditor.run([plugin], {"heavy": 20})

    assert cache_size == 1
    assert second["audits"]["cached"].known_bytes == 20
    assert first["audits"]["cached"].imports[0].cost_bytes == 10

    (tmp_path / "cached" / "extra.py").write_text("import other\n", encoding="utf-8")
    third = auditor.run([plugin], {"heavy": 20, "other": 5})
    assert third["audits"]["cached"].files == 2
    assert {item.module for item in third["audits"]["cached"].imports} == {
        "heavy",
        "other",
    }


def test_audit_marks_file_limit_and_preserves_guarded_flag(tmp_path):
    plugin = make_plugin(
        tmp_path,
        "limited",
        "import heavy\n",
    )
    for index in range(3):
        (tmp_path / "limited" / f"{index}.py").write_text("import extra\n", encoding="utf-8")

    result = DependencyAuditor(max_files=2, time_budget_ms=2000).run(
        [plugin],
        {"heavy": 10, "extra": 2},
    )
    audit = result["audits"]["limited"]
    assert audit.truncated is True
    assert audit.files == 2

    guarded = make_plugin(
        tmp_path,
        "guarded",
        "try:\n    import optional\nexcept Exception:\n    pass\n",
    )
    guarded_result = DependencyAuditor().run([guarded], {"optional": 4})
    item = guarded_result["audits"]["guarded"].imports[0]
    assert item.guarded is True
    assert item.to_dict() == {
        "module": "optional",
        "file": "main.py",
        "lineno": 2,
        "guarded": True,
        "cost_bytes": 4,
    }


def test_raw_import_dataclass_is_small_and_serializable():
    item = RawImport("numpy", "sub/main.py", 7, False)
    assert item.top == "numpy"
