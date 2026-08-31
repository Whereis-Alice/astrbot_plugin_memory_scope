"""Object-graph scan that estimates how much memory a plugin actually retains.

tracemalloc answers "who allocated this?"; it cannot answer "who is still
holding it?" when the allocation happened before tracing started.  This module
walks the reference graph from each plugin instance with :func:`gc.get_referents`
and sums :func:`sys.getsizeof`.

Two things keep the walk from swallowing the whole process:

* boundary types (modules, classes, functions, frames, coroutines) are sized but
  never traversed, so a plugin does not get billed for ``httpx`` internals just
  because it imported ``httpx``;
* a deny list of core objects (the AstrBot ``Context`` and everything hanging
  off it, the event loop, the logging tree) is skipped entirely.

Objects reachable from more than one plugin are reported separately as shared
instead of being double counted in the exclusive figure.
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


@dataclass(slots=True)
class RetainedResult:
    exclusive_bytes: int = 0
    exclusive_objects: int = 0
    shared_bytes: int = 0
    shared_objects: int = 0
    truncated: bool = False

    @property
    def total_bytes(self) -> int:
        return self.exclusive_bytes + self.shared_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclusive_bytes": self.exclusive_bytes,
            "exclusive_objects": self.exclusive_objects,
            "shared_bytes": self.shared_bytes,
            "shared_objects": self.shared_objects,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class ScanReport:
    results: dict[str, RetainedResult] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    scanned_objects: int = 0
    truncated: bool = False


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


def _walk(
    roots: Iterable[Any],
    denylist: set[int],
    max_objects: int,
    deadline: float,
    max_depth: int,
) -> tuple[dict[int, int], bool]:
    """Breadth-limited reference walk returning ``{id: size}``."""

    seen: dict[int, int] = {}
    truncated = False
    stack: list[tuple[Any, int]] = [(root, 0) for root in roots if root is not None]
    checks = 0

    while stack:
        checks += 1
        if checks % 512 == 0 and time.monotonic() > deadline:
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


def scan(
    roots_by_plugin: dict[str, list[Any]],
    denylist: set[int],
    limits: ScanLimits | None = None,
) -> ScanReport:
    """Measure retained size per plugin and split exclusive vs shared bytes."""

    limits = limits or ScanLimits()
    started = time.monotonic()
    deadline = started + max(0.05, limits.time_budget_ms / 1000.0)
    report = ScanReport()

    per_plugin: dict[str, dict[int, int]] = {}
    ownership: dict[int, int] = {}
    budget_left = max(1000, limits.max_objects_total)
    truncated_any = False

    for name, roots in roots_by_plugin.items():
        allowance = min(limits.max_objects_per_plugin, budget_left)
        if allowance <= 0 or time.monotonic() > deadline:
            truncated_any = True
            per_plugin[name] = {}
            report.results[name] = RetainedResult(truncated=True)
            continue
        seen, truncated = _walk(
            roots,
            denylist,
            allowance,
            deadline,
            limits.max_depth,
        )
        truncated_any = truncated_any or truncated
        per_plugin[name] = seen
        budget_left -= len(seen)
        for oid in seen:
            ownership[oid] = ownership.get(oid, 0) + 1
        if truncated:
            report.results[name] = RetainedResult(truncated=True)

    for name, seen in per_plugin.items():
        result = report.results.get(name) or RetainedResult()
        for oid, size in seen.items():
            if ownership.get(oid, 1) > 1:
                result.shared_bytes += size
                result.shared_objects += 1
            else:
                result.exclusive_bytes += size
                result.exclusive_objects += 1
        report.results[name] = result
        report.scanned_objects += len(seen)

    report.truncated = truncated_any
    report.elapsed_ms = (time.monotonic() - started) * 1000.0
    return report