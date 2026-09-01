"""Static audit of module-level imports inside plugin source trees.

This is the layer that does not depend on load order at all: it parses every
plugin ``.py`` file, keeps the imports that run at *module* level (the ones that
cost memory the moment AstrBot loads the plugin, whether the feature is ever
used or not) and joins them against the measured per-package cost table from
``import_cost``.

Imports inside a function or method body are exactly the pattern we want and are
deliberately ignored, as are relative imports, the stdlib and ``astrbot`` itself
(the framework is loaded before any plugin, so a plugin never pays for it).
"""

from __future__ import annotations

import ast
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()))
# ``astrbot`` is imported long before the first plugin, ``data``/``packages`` are
# the plugin namespaces themselves.
IGNORED_TOPS = frozenset({"astrbot", "data", "packages", "__future__", "typing_extensions"})
SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    },
)
DEFAULT_MAX_FILES = 400
DEFAULT_TIME_BUDGET_MS = 2000


@dataclass(slots=True)
class RawImport:
    top: str
    relpath: str
    lineno: int
    guarded: bool


@dataclass(slots=True)
class HeavyImport:
    """One module-level import of a package with a known or unknown cost."""

    module: str
    relpath: str
    lineno: int
    guarded: bool
    cost_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "file": self.relpath,
            "lineno": self.lineno,
            "guarded": self.guarded,
            "cost_bytes": self.cost_bytes,
        }


@dataclass(slots=True)
class PluginAudit:
    name: str
    files: int = 0
    imports: list[HeavyImport] = field(default_factory=list)
    known_bytes: int = 0
    unknown_modules: list[str] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "files": self.files,
            "imports": [item.to_dict() for item in self.imports],
            "known_bytes": self.known_bytes,
            "unknown_modules": list(self.unknown_modules),
            "truncated": self.truncated,
            "error": self.error,
        }


def _is_type_checking(test: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` blocks never execute, so they cost nothing."""

    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    node = handler.type
    if node is None:
        return True
    names: list[str] = []
    if isinstance(node, ast.Tuple):
        candidates: Iterable[ast.expr] = node.elts
    else:
        candidates = (node,)
    for item in candidates:
        if isinstance(item, ast.Name):
            names.append(item.id)
        elif isinstance(item, ast.Attribute):
            names.append(item.attr)
    return any(name in {"ImportError", "ModuleNotFoundError", "Exception"} for name in names)


def collect_module_level_imports(tree: ast.Module, relpath: str) -> list[RawImport]:
    """Imports that execute when the module is imported, function bodies excluded."""

    found: list[RawImport] = []

    def walk(body: list[ast.stmt], guarded: bool) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.partition(".")[0]
                    if top:
                        found.append(RawImport(top, relpath, node.lineno, guarded))
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import: the plugin's own code.
                if node.level or not node.module:
                    continue
                top = node.module.partition(".")[0]
                if top:
                    found.append(RawImport(top, relpath, node.lineno, guarded))
            elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                soft = any(_catches_import_error(h) for h in node.handlers)
                walk(node.body, guarded or soft)
                for handler in node.handlers:
                    walk(handler.body, True)
                walk(node.orelse, guarded or soft)
                walk(node.finalbody, guarded)
            elif isinstance(node, ast.If):
                if _is_type_checking(node.test):
                    continue
                walk(node.body, guarded)
                walk(node.orelse, guarded)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                walk(node.body, guarded)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                walk(node.body, guarded)
                walk(node.orelse, guarded)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    walk(case.body, guarded)
            elif isinstance(node, ast.ClassDef):
                # A class body executes while its containing module is being
                # imported.  Only function/method bodies are lazy here; the
                # recursive walk naturally skips those definitions.
                walk(node.body, guarded)
            # FunctionDef / AsyncFunctionDef bodies are lazy and stay excluded.

    walk(tree.body, False)
    return found


class DependencyAuditor:
    """Scans plugin trees, caches per-directory results by file signature."""

    def __init__(
        self,
        max_files: int = DEFAULT_MAX_FILES,
        time_budget_ms: int = DEFAULT_TIME_BUDGET_MS,
    ) -> None:
        self.max_files = max(1, int(max_files))
        self.time_budget_ms = max(50, int(time_budget_ms))
        self._cache: dict[str, tuple[int, list[RawImport], int, bool]] = {}
        self.last_elapsed_ms = 0.0
        self.last_generated_at = 0.0
        self.last_truncated = False

    # ------------------------------------------------------------------
    def _iter_py_files(self, root: str) -> list[str]:
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if filename.endswith(".py"):
                    files.append(os.path.join(dirpath, filename))
            if len(files) > self.max_files:
                break
        return files

    def _signature(self, files: list[str]) -> int:
        total = len(files)
        for path in files:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            total = (total * 31 + stat.st_mtime_ns + stat.st_size) & 0xFFFFFFFFFFFF
        return total

    def _raw_imports(self, root: str) -> tuple[list[RawImport], int, bool]:
        files = self._iter_py_files(root)
        truncated = len(files) > self.max_files
        files = files[: self.max_files]
        signature = self._signature(files)
        cached = self._cache.get(root)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2], cached[3]
        found: list[RawImport] = []
        for path in files:
            try:
                with open(path, "rb") as handle:
                    source = handle.read()
            except OSError:
                continue
            try:
                tree = ast.parse(source, filename=path)
            except (SyntaxError, ValueError):
                continue
            relpath = os.path.relpath(path, root).replace(os.sep, "/")
            found.extend(collect_module_level_imports(tree, relpath))
        self._cache[root] = (signature, found, len(files), truncated)
        return found, len(files), truncated

    # ------------------------------------------------------------------
    def run(
        self,
        entries: Iterable[Any],
        cost_table: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Audit every entry with a source directory and aggregate the result."""

        costs = cost_table or {}
        started = time.perf_counter()
        deadline = started + self.time_budget_ms / 1000.0
        audits: dict[str, PluginAudit] = {}
        usage: dict[str, dict[str, Any]] = {}
        budget_hit = False
        for entry in entries:
            name = str(getattr(entry, "name", "") or "")
            root = getattr(entry, "root_dir", None)
            if not name:
                continue
            audit = PluginAudit(name=name)
            audits[name] = audit
            if not root or not os.path.isdir(root):
                audit.error = "no_source_dir"
                continue
            if time.perf_counter() > deadline:
                audit.error = "time_budget"
                budget_hit = True
                continue
            try:
                raw, file_count, truncated = self._raw_imports(root)
            except Exception as exc:  # noqa: BLE001 - one bad tree must not stop the audit
                audit.error = type(exc).__name__
                continue
            audit.files = file_count
            audit.truncated = truncated
            own_dir = os.path.basename(os.path.normpath(root))
            self._fill_audit(audit, raw, costs, own_dir, usage)
        self.last_elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        self.last_generated_at = time.time()
        self.last_truncated = budget_hit
        return {
            "audits": audits,
            "opportunities": self._opportunities(usage),
            "elapsed_ms": self.last_elapsed_ms,
            "generated_at": self.last_generated_at,
            "time_budget_hit": budget_hit,
            "cost_table_size": len(costs),
        }

    def _fill_audit(
        self,
        audit: PluginAudit,
        raw: list[RawImport],
        costs: dict[str, int],
        own_dir: str,
        usage: dict[str, dict[str, Any]],
    ) -> None:
        seen: dict[str, HeavyImport] = {}
        for item in raw:
            top = item.top
            if (
                top in STDLIB_MODULES
                or top in IGNORED_TOPS
                or top == own_dir
                or top == audit.name
            ):
                continue
            existing = seen.get(top)
            if existing is not None:
                # An unguarded import anywhere beats a guarded one elsewhere.
                if existing.guarded and not item.guarded:
                    existing.guarded = False
                    existing.relpath = item.relpath
                    existing.lineno = item.lineno
                continue
            cost = costs.get(top)
            heavy = HeavyImport(
                module=top,
                relpath=item.relpath,
                lineno=item.lineno,
                guarded=item.guarded,
                cost_bytes=cost,
            )
            seen[top] = heavy
        audit.imports = sorted(
            seen.values(),
            key=lambda item: (item.cost_bytes or -1, item.module),
            reverse=True,
        )
        audit.known_bytes = sum(item.cost_bytes or 0 for item in audit.imports)
        audit.unknown_modules = sorted(
            item.module for item in audit.imports if item.cost_bytes is None
        )
        for heavy in audit.imports:
            bucket = usage.setdefault(
                heavy.module,
                {"cost_bytes": heavy.cost_bytes, "plugins": [], "guarded": 0},
            )
            bucket["plugins"].append(audit.name)
            if heavy.guarded:
                bucket["guarded"] = int(bucket["guarded"]) + 1

    @staticmethod
    def _opportunities(usage: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Per package: what it costs and who would all have to go lazy.

        The cost is only recovered when *every* listed plugin stops importing it
        at module level, which is why the plugin list is part of the row instead
        of the saving being split between them.
        """

        rows = [
            {
                "module": module,
                "cost_bytes": data["cost_bytes"],
                "plugins": sorted(data["plugins"]),
                "shared_by": len(data["plugins"]),
                "guarded": data["guarded"],
            }
            for module, data in usage.items()
        ]
        rows.sort(key=lambda row: (row["cost_bytes"] or -1, row["shared_by"]), reverse=True)
        return rows
