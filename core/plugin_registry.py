"""Resolve AstrBot plugin identities and map source paths back to plugins.

Allocation samples only carry file names, therefore attribution needs a fast
path -> plugin lookup.  Every AstrBot plugin lives in its own directory
(``data/plugins/<dir>`` for user plugins, ``packages/<dir>`` for the reserved
ones) and the module file is always ``main.py`` or ``<dir>.py`` directly inside
it, so the parent directory of the module file is a safe plugin root.
"""

from __future__ import annotations

import inspect
import os
import sys
import sysconfig
from dataclasses import dataclass, field
from typing import Any

BUCKET_CORE = "astrbot_core"
BUCKET_STDLIB = "python_stdlib"
BUCKET_OTHER = "other"
BUCKET_UNKNOWN = "unknown"
LIB_PREFIX = "lib:"


def normalize(path: str) -> str:
    """Return a comparable absolute path (case-insensitive on Windows)."""

    try:
        return os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return os.path.normcase(path)


def _as_dir_key(path: str) -> str:
    key = normalize(path)
    return key if key.endswith(os.sep) else key + os.sep


@dataclass(slots=True)
class PluginEntry:
    """A single installed plugin plus everything needed to measure it."""

    name: str
    display_name: str
    version: str
    author: str
    activated: bool
    reserved: bool
    module_path: str | None
    root_dir: str | None
    star_cls: Any | None = None
    module: Any | None = None
    submodules: list[Any] = field(default_factory=list)

    @property
    def has_instance(self) -> bool:
        return self.star_cls is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "author": self.author,
            "activated": self.activated,
            "reserved": self.reserved,
            "root_dir": self.root_dir,
            "module_path": self.module_path,
            "has_instance": self.has_instance,
        }


class PluginRegistry:
    """Snapshot of the installed plugins with a path -> plugin resolver."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._entries: dict[str, PluginEntry] = {}
        self._dir_index: list[tuple[str, str]] = []
        self._path_cache: dict[str, str | None] = {}
        self._lib_roots: list[tuple[str, str]] = []
        self._core_root: str | None = None
        self._stdlib_roots: list[str] = []
        self._resolve_static_roots()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _resolve_static_roots(self) -> None:
        try:
            import astrbot

            core_file = getattr(astrbot, "__file__", None)
            if core_file:
                self._core_root = _as_dir_key(os.path.dirname(core_file))
        except Exception:  # pragma: no cover - astrbot is always importable here
            self._core_root = None

        lib_roots: list[tuple[str, str]] = []
        for key in ("purelib", "platlib"):
            try:
                path = sysconfig.get_path(key)
            except (KeyError, OSError):
                path = None
            if path:
                lib_roots.append((_as_dir_key(path), LIB_PREFIX))
        for path in list(sys.path):
            if path and ("site-packages" in path or "dist-packages" in path):
                lib_roots.append((_as_dir_key(path), LIB_PREFIX))
        seen: set[str] = set()
        self._lib_roots = []
        for key, prefix in lib_roots:
            if key not in seen:
                seen.add(key)
                self._lib_roots.append((key, prefix))

        stdlib: list[str] = []
        for key in ("stdlib", "platstdlib"):
            try:
                path = sysconfig.get_path(key)
            except (KeyError, OSError):
                path = None
            if path:
                stdlib.append(_as_dir_key(path))
        self._stdlib_roots = sorted(set(stdlib), key=len, reverse=True)

    def refresh(self) -> None:
        """Rebuild the plugin snapshot from AstrBot's star registry."""

        entries: dict[str, PluginEntry] = {}
        for meta in self._iter_star_metadata():
            entry = self._build_entry(meta)
            if entry is None:
                continue
            # Duplicated names should not happen, but keep the activated one.
            existing = entries.get(entry.name)
            if existing is not None and existing.activated and not entry.activated:
                continue
            entries[entry.name] = entry

        self._entries = entries
        self._dir_index = sorted(
            (
                (_as_dir_key(entry.root_dir), entry.name)
                for entry in entries.values()
                if entry.root_dir
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._path_cache.clear()
        self._attach_submodules()

    def _iter_star_metadata(self) -> list[Any]:
        getter = getattr(self._context, "get_all_stars", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:
            return []

    def _build_entry(self, meta: Any) -> PluginEntry | None:
        name = str(getattr(meta, "name", "") or "").strip()
        root_dir_name = str(getattr(meta, "root_dir_name", "") or "").strip()
        canonical = name or root_dir_name
        if not canonical:
            return None
        module = getattr(meta, "module", None)
        star_cls = getattr(meta, "star_cls", None)
        module_path = getattr(meta, "module_path", None)
        return PluginEntry(
            name=canonical,
            display_name=str(
                getattr(meta, "display_name", None) or canonical,
            ),
            version=str(getattr(meta, "version", "") or ""),
            author=str(getattr(meta, "author", "") or ""),
            activated=bool(getattr(meta, "activated", True)),
            reserved=bool(getattr(meta, "reserved", False)),
            module_path=str(module_path) if module_path else None,
            root_dir=self._resolve_root_dir(meta, module, star_cls),
            star_cls=star_cls,
            module=module,
        )

    def _resolve_root_dir(
        self,
        meta: Any,
        module: Any,
        star_cls: Any,
    ) -> str | None:
        candidates: list[str] = []
        module_file = getattr(module, "__file__", None)
        if module_file:
            candidates.append(module_file)
        module_path = getattr(meta, "module_path", None)
        if module_path:
            sys_module = sys.modules.get(str(module_path))
            sys_file = getattr(sys_module, "__file__", None)
            if sys_file:
                candidates.append(sys_file)
        for target in (star_cls, getattr(meta, "star_cls_type", None)):
            if target is None:
                continue
            try:
                candidates.append(inspect.getfile(type(target) if not isinstance(target, type) else target))
            except (TypeError, OSError):
                continue

        for candidate in candidates:
            directory = os.path.dirname(normalize(candidate))
            if directory and os.path.isdir(directory):
                return directory
        return None

    def _attach_submodules(self) -> None:
        """Collect loaded submodules per plugin (module-level caches live there)."""

        for entry in self._entries.values():
            entry.submodules = []
        if not self._dir_index:
            return
        for module in list(sys.modules.values()):
            module_file = getattr(module, "__file__", None)
            if not module_file:
                continue
            owner = self.resolve_path(module_file)
            if owner is None:
                continue
            entry = self._entries.get(owner)
            if entry is None or module is entry.module:
                continue
            entry.submodules.append(module)

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------
    @property
    def entries(self) -> list[PluginEntry]:
        return list(self._entries.values())

    def get(self, name: str) -> PluginEntry | None:
        return self._entries.get(name)

    def resolve_path(self, filename: str | None) -> str | None:
        """Return the plugin name owning ``filename``, or None."""

        if not filename:
            return None
        cached = self._path_cache.get(filename, False)
        if cached is not False:
            return cached  # type: ignore[return-value]
        owner: str | None = None
        if not filename.startswith("<"):
            key = normalize(filename)
            for dir_key, name in self._dir_index:
                if key.startswith(dir_key):
                    owner = name
                    break
        self._path_cache[filename] = owner
        return owner

    def classify_path(self, filename: str | None) -> str:
        """Bucket a non-plugin path for the "everything else" breakdown."""

        if not filename or filename.startswith("<"):
            return BUCKET_UNKNOWN
        key = normalize(filename)
        # AstrBot itself may be installed into site-packages, so the core root
        # has to win over the generic library bucket.
        if self._core_root and key.startswith(self._core_root):
            return BUCKET_CORE
        for dir_key, _prefix in self._lib_roots:
            if key.startswith(dir_key):
                rest = key[len(dir_key) :]
                top = rest.split(os.sep, 1)[0]
                if top.endswith(".py"):
                    top = top[:-3]
                return f"{LIB_PREFIX}{top}" if top else BUCKET_OTHER
        for dir_key in self._stdlib_roots:
            if key.startswith(dir_key):
                return BUCKET_STDLIB
        return BUCKET_OTHER