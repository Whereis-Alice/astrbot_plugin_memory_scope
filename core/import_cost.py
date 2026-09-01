"""Attribute the memory cost of loading plugins with an ``__import__`` hook.

Why not tracemalloc?  v1.0.0 called ``tracemalloc.start(12)`` from the plugin
constructor, so every plugin loaded afterwards paid a 12-frame traceback on
every single allocation: 39 s of plugin loading became 1265 s on a 2-core /
1.6 GB host, and the Dashboard port stayed closed for 25 minutes.  See README.

This probe wraps ``builtins.__import__`` instead, reads the process RSS before
and after each *first* import of a module and books the difference.  One
measured import costs two RSS reads (~3 us each) plus four clock reads, so the
whole startup pays tens of milliseconds -- three orders of magnitude less than
tracemalloc, and nothing at all once loading is over and the hook is removed.

What the numbers mean
---------------------
RSS deltas are page-granular, never shrink when the allocator holds on to
freed blocks, and absorb whatever other threads allocate at the same moment.
They answer "roughly what did importing this cost", not "here is the exact
bill".  A shared dependency is booked to whoever imported it *first*, which is
what ``PackageCost.first_importer`` records: the second plugin to
``import pillow`` honestly measures ~0 because the pages already exist.
"""

from __future__ import annotations

import builtins
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# star_manager imports user plugins as ``data.plugins.<dir>.main`` and reserved
# ones as ``packages.<dir>.main``; group 1 is always the plugin directory name.
PLUGIN_MODULE_RE = re.compile(
    r"^(?:data\.plugins|astrbot\.builtin_stars|packages)\.([^.]+)(?:\.|$)"
)

# The hook is intended to expose third-party import costs, not charge a plugin
# for a late stdlib/framework import. ``sys.stdlib_module_names`` is available
# on all Python versions supported by AstrBot; keep the fallback for older
# embedded interpreters. ``data`` and ``packages`` are AstrBot's plugin
# namespace packages, not dependencies in their own right.
_STDLIB_TOPS = frozenset(getattr(sys, "stdlib_module_names", ()))
IGNORED_TOPS = _STDLIB_TOPS | frozenset(
    {"astrbot", "data", "packages", "__future__"},
)

# A pass-through hook call only runs the classifier, measured at 1-2 us.  Timing
# it would roughly double its price, so the reported overhead uses this constant
# for pass-throughs and real clock readings for measured imports.
PASSTHROUGH_NS = 1500
PASSTHROUGH_BUDGET_CHECK_EVERY = 1024

DEFAULT_MAX_OVERHEAD_MS = 5000.0


class RssReader:
    """Cheapest available process-RSS reader.

    ``/proc/self/statm`` read through ``os.pread`` wins where it exists: one
    syscall, no allocation beyond the returned bytes, and atomic with respect to
    other threads.  psutil is the portable fallback.
    """

    def __init__(self) -> None:
        self.source = "unavailable"
        self._fd: int | None = None
        self._page_size = 4096
        self._process: Any = None
        if not self._try_proc():
            self._try_psutil()

    def _try_proc(self) -> bool:
        path = "/proc/self/statm"
        if not os.path.exists(path):
            return False
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return False
        try:
            self._page_size = int(os.sysconf("SC_PAGE_SIZE"))
        except (AttributeError, ValueError, OSError):
            self._page_size = 4096
        self._fd = fd
        try:
            self._read_proc()
        except Exception:  # noqa: BLE001 - fall back rather than fail
            os.close(fd)
            self._fd = None
            return False
        self.source = "proc"
        return True

    def _try_psutil(self) -> bool:
        try:
            import psutil  # type: ignore[import-not-found]

            self._process = psutil.Process(os.getpid())
            int(self._process.memory_info().rss)
        except Exception:  # noqa: BLE001 - psutil is optional
            self._process = None
            return False
        self.source = "psutil"
        return True

    def _read_proc(self) -> int:
        # field 2 of statm is the resident set size, in pages
        return int(os.pread(self._fd or 0, 128, 0).split()[1]) * self._page_size

    @property
    def available(self) -> bool:
        return self.source not in {"unavailable", "closed"}

    @property
    def closed(self) -> bool:
        """Whether this reader has been closed and must be recreated."""

        return self.source == "closed"

    def read(self) -> int:
        try:
            if self._fd is not None:
                return self._read_proc()
            if self._process is not None:
                return int(self._process.memory_info().rss)
        except Exception:  # noqa: BLE001 - a probe must not raise
            return 0
        return 0

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self._process = None
        self.source = "closed"


@dataclass(slots=True)
class PackageCost:
    """Measured cost of first-importing one top-level third-party package."""

    name: str
    bytes: int = 0
    self_bytes: int = 0
    wall_ms: float = 0.0
    modules: int = 0
    imports: int = 0
    first_importer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bytes": self.bytes,
            "self_bytes": self.self_bytes,
            "wall_ms": round(self.wall_ms, 1),
            "modules": self.modules,
            "imports": self.imports,
            "first_importer": self.first_importer,
        }


@dataclass(slots=True)
class PluginImportCost:
    """Measured cost of importing one plugin package, dependencies included."""

    name: str
    bytes: int = 0
    self_bytes: int = 0
    wall_ms: float = 0.0
    modules: int = 0
    packages: list[str] = field(default_factory=list)
    measured_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bytes": self.bytes,
            "self_bytes": self.self_bytes,
            "wall_ms": round(self.wall_ms, 1),
            "modules": self.modules,
            "packages": list(self.packages),
            "measured_at": self.measured_at or None,
        }


@dataclass(slots=True)
class _Frame:
    kind: str
    key: str
    rss0: int
    t0: int
    mods0: int
    child_bytes: int = 0


class ImportCostLedger:
    """Wraps ``builtins.__import__`` and books RSS deltas per plugin/package."""

    def __init__(self, max_overhead_ms: float = DEFAULT_MAX_OVERHEAD_MS) -> None:
        self.max_overhead_ms = float(max_overhead_ms)
        self.rss = RssReader()
        self.plugins: dict[str, PluginImportCost] = {}
        self.packages: dict[str, PackageCost] = {}
        self.installed_at: float | None = None
        self.finished_at: float | None = None
        self.rss_at_install = 0
        self.rss_at_finish = 0
        self.degraded_reason: str | None = None
        self._installed = False
        self._original: Callable[..., Any] = builtins.__import__
        # ``self._hook`` builds a fresh bound method on every attribute access,
        # so identity checks against it always fail. Keep one stable reference.
        self._wrapper: Callable[..., Any] = self._hook
        self._local = threading.local()
        self._active_plugins: set[str] = set()
        self._active: set[str] = set()
        self._seen: set[str] = set()
        self._overhead_ns = 0
        self._passthrough_calls = 0
        self._measured_calls = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def degraded(self) -> bool:
        return self.degraded_reason is not None

    @property
    def overhead_ms(self) -> float:
        """Everything this probe cost the process, pass-throughs estimated."""

        total_ns = self._overhead_ns + self._passthrough_calls * PASSTHROUGH_NS
        return total_ns / 1_000_000.0

    def install(self) -> bool:
        """Take over ``builtins.__import__``; returns False when unsupported."""

        if self._installed:
            return True
        # A previous run may have detached itself after an overhead/resource
        # failure.  Re-wrapping in that state would add ambient cost while the
        # ledger is already known to be incomplete; require a fresh process (or
        # an explicit test reset) instead.
        if self.degraded_reason is not None:
            return False
        # A runtime reload can reuse the process-wide singleton after the
        # previous instance uninstalled it.  ``uninstall`` closes the /proc fd,
        # so reopen a fresh reader instead of silently recording zero deltas.
        if bool(getattr(self.rss, "closed", False)):
            self.rss = RssReader()
        if not self.rss.available:
            self.degraded_reason = "no_rss_reader"
            return False
        self._original = builtins.__import__
        self.rss_at_install = self.rss.read()
        self.installed_at = time.time()
        self._installed = True
        builtins.__import__ = self._wrapper
        return True

    def uninstall(self) -> bool:
        """Restore the previous ``__import__``; returns False if not ours.

        ``_check_budget`` can detach the wrapper before the lifecycle hook gets
        a chance to call us.  In that case this method is still responsible for
        closing the tiny RSS reader resource, even though there is no active
        wrapper left to restore.
        """

        if not self._installed:
            if self.rss.available:
                self.rss.close()
            return False
        return self._detach(close=True)

    def _detach(self, reason: str | None = None, *, close: bool) -> bool:
        """Remove our wrapper without ever disturbing a newer wrapper."""

        if reason and self.degraded_reason is None:
            self.degraded_reason = reason
        # Flip this first: from here on the hook is a pure pass-through even if
        # another wrapper keeps calling its bound method.
        self._installed = False
        self.finished_at = time.time()
        self.rss_at_finish = self.rss.read()
        restored = builtins.__import__ is self._wrapper
        if restored:
            builtins.__import__ = self._original
        else:
            # Another plugin wrapped us afterwards; unwrapping now would silently
            # drop their hook, so leave the chain alone and say so.
            self.degraded_reason = self.degraded_reason or "hook_chain_changed"
        if close:
            self.rss.close()
        return restored

    # ------------------------------------------------------------------
    # the hook
    # ------------------------------------------------------------------
    def _hook(
        self,
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        original = self._original
        # Relative imports always happen inside a module we are already timing,
        # so their cost is captured by the enclosing frame.
        if not self._installed or self.degraded_reason is not None or level != 0:
            self._count_passthrough()
            return original(name, globals, locals, fromlist, level)
        try:
            entered = time.perf_counter_ns()
            kind, key = self._classify(name)
        except Exception:  # noqa: BLE001 - never break the import system
            self._fail("classify_error")
            return original(name, globals, locals, fromlist, level)
        if kind is None:
            self._count_passthrough()
            return original(name, globals, locals, fromlist, level)
        return self._measure(
            kind, key, entered, name, globals, locals, fromlist, level,
        )

    def _classify(self, name: str) -> tuple[str | None, str]:
        """Decide whether ``name`` opens a plugin frame, a package frame or none."""

        match = PLUGIN_MODULE_RE.match(name)
        if match is not None:
            key = match.group(1)
            # Submodules imported while the plugin frame is open are already
            # inside its measurement.
            if key in self._active_plugins or name in sys.modules:
                return None, ""
            return "plugin", key
        top = name.partition(".")[0]
        if (
            not top
            or top in IGNORED_TOPS
            or name in sys.modules
            or top in self._active
            or top in self._seen
        ):
            return None, ""
        return "package", top

    def _measure(
        self,
        kind: str,
        key: str,
        entered: int,
        name: str,
        globals: Any,
        locals: Any,
        fromlist: Any,
        level: int,
    ) -> Any:
        original = self._original
        active = self._active_plugins if kind == "plugin" else self._active
        try:
            stack = self._stack()
            # For package frames ``key`` is always the top-level package name;
            # this keeps ``pkg.submodule`` inside the same active frame.
            active.add(key)
            rss0 = self.rss.read()
            mods0 = len(sys.modules)
            t0 = time.perf_counter_ns()
            self._overhead_ns += t0 - entered
            frame = _Frame(kind=kind, key=key, rss0=rss0, t0=t0, mods0=mods0)
            stack.append(frame)
        except Exception:  # noqa: BLE001
            active.discard(key)
            self._fail("setup_error")
            return original(name, globals, locals, fromlist, level)
        try:
            return original(name, globals, locals, fromlist, level)
        finally:
            try:
                t1 = time.perf_counter_ns()
                stack.pop()
                self._book(frame, t1, stack)
                self._measured_calls += 1
            except Exception:  # noqa: BLE001
                self._fail("book_error")
            finally:
                active.discard(key)
                if kind == "package":
                    self._seen.add(key)
                self._overhead_ns += time.perf_counter_ns() - t1
                try:
                    self._check_budget()
                except Exception:  # noqa: BLE001 - budget protection is best effort
                    self._fail("budget_check_error")

    def _count_passthrough(self) -> None:
        self._passthrough_calls += 1
        if (
            self._installed
            and self._passthrough_calls % PASSTHROUGH_BUDGET_CHECK_EVERY == 0
        ):
            self._check_budget()

    def _stack(self) -> list[_Frame]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    def _book(self, frame: _Frame, t1: int, stack: list[_Frame]) -> None:
        gross = max(0, self.rss.read() - frame.rss0)
        wall_ms = (t1 - frame.t0) / 1_000_000.0
        modules = max(0, len(sys.modules) - frame.mods0)
        # Nested measured imports are counted in ``gross`` too, so keep an
        # exclusive figure around for anything that sums rows.
        own = max(0, gross - frame.child_bytes)
        if stack:
            stack[-1].child_bytes += gross
        if frame.kind == "plugin":
            record = self.plugins.get(frame.key)
            if record is None:
                record = PluginImportCost(name=frame.key, measured_at=time.time())
                self.plugins[frame.key] = record
            # Dependencies are nested inside the plugin frame, so their
            # ``_book`` runs before the plugin record itself exists.  Reconcile
            # the first-importer links after creating the record; otherwise the
            # plugin's package list is empty even though the package ledger is
            # correctly attributed.
            for package in self.packages.values():
                if (
                    package.first_importer == frame.key
                    and package.name not in record.packages
                ):
                    record.packages.append(package.name)
            record.bytes += gross
            record.self_bytes += own
            record.wall_ms += wall_ms
            record.modules += modules
            return
        package = self.packages.get(frame.key)
        if package is None:
            owner = self._current_plugin(stack)
            package = PackageCost(name=frame.key, first_importer=owner)
            self.packages[frame.key] = package
            if owner is not None:
                plugin = self.plugins.get(owner)
                if plugin is not None and frame.key not in plugin.packages:
                    plugin.packages.append(frame.key)
        package.bytes += gross
        package.self_bytes += own
        package.wall_ms += wall_ms
        package.modules += modules
        package.imports += 1

    @staticmethod
    def _current_plugin(stack: list[_Frame]) -> str | None:
        for frame in reversed(stack):
            if frame.kind == "plugin":
                return frame.key
        return None

    def _fail(self, reason: str) -> None:
        if self.degraded_reason is None:
            self.degraded_reason = reason

    def _check_budget(self) -> None:
        """Self-police: a monitor that costs real time must switch itself off."""

        if self.max_overhead_ms <= 0:
            return
        if self.overhead_ms > self.max_overhead_ms:
            if not self._installed:
                self.degraded_reason = self.degraded_reason or "overhead_budget_exceeded"
                return
            # Restore the original import function immediately.  Do not close
            # the RSS reader here: this check runs from an import-finally block,
            # and an enclosing plugin frame may still need one final RSS read.
            # The normal lifecycle teardown calls ``uninstall`` again and closes
            # it; the unclosed object is only a tiny fallback fd in the meantime.
            self._detach("overhead_budget_exceeded", close=False)

    # ------------------------------------------------------------------
    # readers
    # ------------------------------------------------------------------
    def measured(self, plugin: str) -> bool:
        return plugin in self.plugins

    def plugin_cost(self, plugin: str) -> PluginImportCost | None:
        return self.plugins.get(plugin)

    def cost_table(self) -> dict[str, int]:
        """``{top_level_package: bytes}``, for the dependency audit."""

        return {name: cost.bytes for name, cost in self.packages.items()}

    def packages_sorted(self) -> list[PackageCost]:
        return sorted(
            self.packages.values(),
            key=lambda item: (item.bytes, item.wall_ms),
            reverse=True,
        )

    def snapshot(self, package_limit: int = 40) -> dict[str, Any]:
        ordered = self.packages_sorted()
        head = ordered[:package_limit] if package_limit > 0 else ordered
        finish = self.rss_at_finish or self.rss.read()
        return {
            "installed": self._installed,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "rss_source": self.rss.source,
            "installed_at": self.installed_at,
            "finished_at": self.finished_at,
            "rss_at_install": self.rss_at_install,
            "rss_at_finish": self.rss_at_finish or None,
            "rss_growth_bytes": max(0, finish - self.rss_at_install),
            "overhead_ms": round(self.overhead_ms, 2),
            "calls": self._passthrough_calls + self._measured_calls,
            "measured_calls": self._measured_calls,
            "plugin_count": len(self.plugins),
            "plugin_bytes": sum(item.self_bytes for item in self.plugins.values()),
            "package_count": len(self.packages),
            "package_bytes": sum(item.self_bytes for item in self.packages.values()),
            "packages": [item.to_dict() for item in head],
            "packages_truncated": max(0, len(ordered) - len(head)),
        }


_LEDGER: ImportCostLedger | None = None


def get_ledger(max_overhead_ms: float = DEFAULT_MAX_OVERHEAD_MS) -> ImportCostLedger:
    """Process-wide singleton, so a plugin reload cannot install two hooks."""

    global _LEDGER
    if _LEDGER is None:
        _LEDGER = ImportCostLedger(max_overhead_ms)
    else:
        _LEDGER.max_overhead_ms = float(max_overhead_ms)
    return _LEDGER


def reset_ledger() -> None:
    """Drop the singleton (tests only); uninstalls it first when needed."""

    global _LEDGER
    if _LEDGER is not None:
        _LEDGER.uninstall()
    _LEDGER = None
