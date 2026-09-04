"""Object-graph scan that estimates how much memory a plugin actually retains.

tracemalloc answers "who allocated this?"; it cannot answer "who is still
holding it?" when the allocation happened before tracing started -- and turning
it on is not free: v1.0.0 of this plugin started tracing in its constructor and
stretched AstrBot's plugin load from 39 s to 1265 s.  So this module walks the
reference graph from each plugin instance with gc.get_referents and sums
sys.getsizeof instead.  It runs on demand, costs nothing when idle, and never
instruments the allocator.

Three things keep the walk from swallowing the whole process:

* boundary types (modules, classes, functions, frames, coroutines) are sized but
  never traversed, so a plugin does not get billed for httpx internals just
  because it imported httpx;
* a deny list of core objects (the AstrBot Context and everything hanging off
  it, the event loop, the logging tree) is skipped entirely;
* the object budget is shared fairly across plugins and rotates its starting
  point every round, so one huge plugin cannot starve the other 126.

Objects reachable from more than one plugin are reported separately as shared,
split 1/N between the holders so that the per-plugin numbers still add up to the
measured total.

Latency: the walk holds the GIL, so a single 3 s pass would stall every handler
for 3 s.  The pacer breaks it into ~15 ms slices with a duty cycle, which turns
one long stall into a series of pauses shorter than CPython's own thread switch
interval (5 ms) is long.
"""

from __future__ import annotations

import gc
import sys
import time
import types
import weakref
from dataclasses import dataclass, field
from typing import Any, Iterable

BOUNDARY_TYPES: tuple[type, ...] = (
    types.ModuleType,
    type,
    types.FunctionType,
    types.LambdaType,
    types.MethodType,
    types.BuiltinFunctionType,
    types.BuiltinMethodType,
    types.MethodWrapperType,
    types.WrapperDescriptorType,
    types.MethodDescriptorType,
    types.ClassMethodDescriptorType,
    types.GetSetDescriptorType,
    types.MemberDescriptorType,
    types.CodeType,
    types.FrameType,
    types.TracebackType,
    types.MappingProxyType,
    types.GeneratorType,
    types.CoroutineType,
    types.AsyncGeneratorType,
    weakref.ref,
    weakref.ProxyType,
    weakref.CallableProxyType,
    super,
)


@dataclass(slots=True)
class ScanLimits:
    """Safety valves; a monitoring plugin must never stall the bot."""

    max_objects_per_plugin: int = 120_000
    max_objects_total: int = 400_000
    time_budget_ms: int = 3000
    max_depth: int = 60
    #: How long the scan may hold the GIL before yielding.
    slice_ms: int = 15
    #: Percentage of wall time the scan is allowed to occupy.  25 means "work
    #: 15 ms, sleep 45 ms", so the scan takes 4x longer in wall time but leaves
    #: three quarters of every window to the bot.
    duty_percent: int = 25

    @property
    def slice_seconds(self) -> float:
        return max(0.001, self.slice_ms / 1000.0)

    @property
    def sleep_seconds(self) -> float:
        duty = self.duty_percent
        if duty >= 100:
            return 0.0
        duty = max(1, duty)
        return self.slice_seconds * (100 - duty) / duty

    @property
    def work_budget_seconds(self) -> float:
        """Budget of CPU time, not wall time -- sleeps do not count."""

        return max(0.05, self.time_budget_ms / 1000.0)


@dataclass(slots=True)
class RetainedResult:
    exclusive_bytes: int = 0
    exclusive_objects: int = 0
    #: Shared bytes after 1/N attribution.  Kept as a float while accumulating
    #: so that N plugins sharing one object still sum back to its real size.
    shared_bytes: float = 0.0
    shared_objects: int = 0
    #: Full size of every shared object, undivided.  Useful for "who else is
    #: holding this?" drill-downs, but summing it across plugins double counts.
    shared_full_bytes: int = 0
    truncated: bool = False
    scanned_objects: int = 0

    @property
    def shared_share_bytes(self) -> int:
        return int(round(self.shared_bytes))

    @property
    def total_bytes(self) -> int:
        return self.exclusive_bytes + self.shared_share_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclusive_bytes": self.exclusive_bytes,
            "exclusive_objects": self.exclusive_objects,
            "shared_bytes": self.shared_share_bytes,
            "shared_objects": self.shared_objects,
            "shared_full_bytes": self.shared_full_bytes,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "scanned_objects": self.scanned_objects,
        }


@dataclass(slots=True)
class ScanReport:
    results: dict[str, RetainedResult] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    #: CPU time actually spent walking, excluding duty-cycle sleeps.
    work_ms: float = 0.0
    scanned_objects: int = 0
    truncated: bool = False
    plugin_count: int = 0
    complete_count: int = 0
    truncated_count: int = 0
    slices: int = 0
    #: Which plugin the round-robin started from this round.
    start_index: int = 0
    exclusive_bytes: int = 0
    shared_bytes: int = 0

    @property
    def measured_bytes(self) -> int:
        return self.exclusive_bytes + self.shared_bytes

    def coverage(self) -> dict[str, Any]:
        return {
            "plugin_count": self.plugin_count,
            "complete_count": self.complete_count,
            "truncated_count": self.truncated_count,
            "scanned_objects": self.scanned_objects,
            "exclusive_bytes": self.exclusive_bytes,
            "shared_bytes": self.shared_bytes,
            "measured_bytes": self.measured_bytes,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "work_ms": round(self.work_ms, 1),
            "slices": self.slices,
            "start_index": self.start_index,
            "truncated": self.truncated,
        }


def safe_sizeof(obj: Any) -> int:
    try:
        return sys.getsizeof(obj)
    except Exception:
        return 0


def is_boundary(obj: Any) -> bool:
    return isinstance(obj, BOUNDARY_TYPES)


def build_denylist(
    context: Any,
    extra_roots: Iterable[Any] = (),
    depth: int = 3,
) -> set[int]:
    """Collect ids of objects that belong to AstrBot core, not to a plugin."""

    denied: set[int] = set()

    def add(obj: Any, remaining: int) -> None:
        if obj is None:
            return
        oid = id(obj)
        if oid in denied:
            return
        denied.add(oid)
        if remaining <= 0:
            return
        obj_dict = getattr(obj, "__dict__", None)
        if isinstance(obj_dict, dict):
            denied.add(id(obj_dict))
            for value in list(obj_dict.values()):
                add(value, remaining - 1)
        if isinstance(obj, dict):
            for value in list(obj.values()):
                add(value, remaining - 1)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for value in list(obj):
                add(value, remaining - 1)

    if context is not None:
        add(context, depth)
    denied.add(id(sys.modules))

    try:
        import asyncio

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            # The loop transitively references every transport and task, so it
            # must never be traversed as if it were plugin memory.
            add(running, 2)
    except Exception:
        pass

    try:
        import logging

        add(logging.root, 1)
        manager = getattr(logging.Logger, "manager", None)
        add(manager, 1)
        logger_dict = getattr(manager, "loggerDict", None)
        if isinstance(logger_dict, dict):
            denied.add(id(logger_dict))
            for value in list(logger_dict.values()):
                add(value, 1)
    except Exception:
        pass

    for root in extra_roots:
        add(root, 0)

    return denied


@dataclass(slots=True)
class _Pacer:
    """Duty-cycle scheduler that yields the GIL between work slices."""

    slice_seconds: float
    sleep_seconds: float
    work_budget: float
    work_spent: float = 0.0
    slice_started: float = 0.0
    slices: int = 0

    def start(self) -> None:
        self.slice_started = time.monotonic()

    def tick(self) -> bool:
        """Return True when the CPU budget is gone; sleep between slices."""

        now = time.monotonic()
        elapsed = now - self.slice_started
        if elapsed < self.slice_seconds:
            return False
        self.work_spent += elapsed
        self.slices += 1
        if self.work_spent >= self.work_budget:
            return True
        if self.sleep_seconds > 0:
            # The only place this scan gives other coroutines a chance to run.
            time.sleep(self.sleep_seconds)
        self.slice_started = time.monotonic()
        return False

    def close(self) -> None:
        self.work_spent += max(0.0, time.monotonic() - self.slice_started)
        self.slice_started = time.monotonic()

    @property
    def exhausted(self) -> bool:
        return self.work_spent >= self.work_budget


def _walk(
    roots: Iterable[Any],
    denylist: set[int],
    max_objects: int,
    max_depth: int,
    pacer: _Pacer,
) -> tuple[dict[int, int], bool]:
    """Breadth-limited reference walk returning a mapping of id -> size."""

    seen: dict[int, int] = {}
    truncated = False
    stack: list[tuple[Any, int]] = [(root, 0) for root in roots if root is not None]
    checks = 0

    while stack:
        checks += 1
        if checks % 512 == 0 and pacer.tick():
            truncated = True
            break
        if len(seen) >= max_objects:
            truncated = True
            break

        obj, depth = stack.pop()
        oid = id(obj)
        if oid in seen or oid in denylist:
            continue
        seen[oid] = safe_sizeof(obj)
        if depth >= max_depth or is_boundary(obj):
            continue
        try:
            referents = gc.get_referents(obj)
        except Exception:
            continue
        next_depth = depth + 1
        for referent in referents:
            if id(referent) not in seen:
                stack.append((referent, next_depth))

    return seen, truncated


class RetainedScanner:
    """Stateful scanner: fair object budget plus a rotating start plugin.

    A single pass must stay atomic -- exclusive-vs-shared can only be decided
    while every plugin's object set is known at the same instant, and keeping
    127 id-to-size maps alive between samples would cost tens of megabytes on
    the kind of box this plugin exists for.  Fairness is therefore handled
    inside one pass, and the *starting* plugin rotates so that a plugin unlucky
    enough to sit past the budget cutoff gets measured on the next round.
    """

    def __init__(self, limits: ScanLimits | None = None) -> None:
        self.limits = limits or ScanLimits()
        self.rounds = 0
        self._cursor = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    def scan(
        self,
        roots_by_plugin: dict[str, list[Any]],
        denylist: set[int],
        limits: ScanLimits | None = None,
    ) -> ScanReport:
        """Measure retained size per plugin and split exclusive vs shared."""

        limits = limits or self.limits
        names = list(roots_by_plugin)
        report = ScanReport(plugin_count=len(names))
        if not names:
            return report

        started = time.monotonic()
        start_index = self._cursor % len(names)
        report.start_index = start_index
        order = names[start_index:] + names[:start_index]
        self._cursor = (start_index + 1) % len(names)
        self.rounds += 1

        pacer = _Pacer(
            slice_seconds=limits.slice_seconds,
            sleep_seconds=limits.sleep_seconds,
            work_budget=limits.work_budget_seconds,
        )
        pacer.start()

        per_plugin: dict[str, dict[int, int]] = {}
        ownership: dict[int, int] = {}
        truncated_any = False

        total_budget = max(1000, limits.max_objects_total)
        per_plugin_cap = max(1, limits.max_objects_per_plugin)
        # Every plugin is promised an equal slice of the total; whatever a small
        # plugin does not use rolls forward as credit for the next one.
        fair = max(2000, total_budget // len(names))
        budget_left = total_budget
        credit = 0

        for name in order:
            allowance = min(per_plugin_cap, fair + credit, budget_left)
            if allowance <= 0 or pacer.exhausted:
                truncated_any = True
                per_plugin[name] = {}
                report.results[name] = RetainedResult(truncated=True)
                continue
            seen, truncated = _walk(
                roots_by_plugin.get(name) or [],
                denylist,
                allowance,
                limits.max_depth,
                pacer,
            )
            truncated_any = truncated_any or truncated
            per_plugin[name] = seen
            used = len(seen)
            budget_left -= used
            credit = min(per_plugin_cap, max(0, fair + credit - used))
            for oid in seen:
                ownership[oid] = ownership.get(oid, 0) + 1
            report.results[name] = RetainedResult(truncated=truncated)

        pacer.close()

        for name, seen in per_plugin.items():
            result = report.results.get(name) or RetainedResult()
            for oid, size in seen.items():
                holders = ownership.get(oid, 1)
                if holders > 1:
                    result.shared_bytes += size / holders
                    result.shared_full_bytes += size
                    result.shared_objects += 1
                else:
                    result.exclusive_bytes += size
                    result.exclusive_objects += 1
            result.scanned_objects = len(seen)
            report.results[name] = result
            report.scanned_objects += len(seen)
            report.exclusive_bytes += result.exclusive_bytes
            report.shared_bytes += result.shared_share_bytes
            if result.truncated:
                report.truncated_count += 1
            else:
                report.complete_count += 1

        report.truncated = truncated_any
        report.work_ms = pacer.work_spent * 1000.0
        report.slices = pacer.slices
        report.elapsed_ms = (time.monotonic() - started) * 1000.0
        return report


def scan(
    roots_by_plugin: dict[str, list[Any]],
    denylist: set[int],
    limits: ScanLimits | None = None,
) -> ScanReport:
    """Stateless convenience wrapper; the collector keeps a scanner instead."""

    return RetainedScanner(limits).scan(roots_by_plugin, denylist, limits)
