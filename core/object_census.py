"""Census of live objects, attributed by the module that defines their type.

``type(obj).__module__`` maps an object to a module, and a module maps to a
plugin directory, so attribution here is unambiguous -- no call stacks, no
"whoever imported it first".  If ``myplugin.cache.Entry`` has 400 000 instances,
it is that plugin holding them, full stop.

Two honest limits
-----------------
1. Only GC-tracked objects are visible.  ``str``, ``bytes``, ``int`` and
   ``float`` are not tracked by the cycle collector, so they never appear here.
   The census total is therefore far below RSS by design; it is a population
   count of containers and instances, not a memory total.
2. ``sys.getsizeof`` is shallow: a dict with a million entries reports its own
   table, while the values are counted separately when they are tracked at all.

Cost: ``gc.get_objects()`` builds one list of every tracked object (about 8 MB
per million objects) and touching them all faults swapped-out pages back in.
That is why the census is opt-in and supports 1/N sampling instead of running on
every background tick.
"""

from __future__ import annotations

import gc
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from .plugin_registry import BUCKET_STDLIB, BUCKET_UNKNOWN

BUCKET_BUILTINS = "builtins"
# Checking the clock on every object would show up in the measurement itself.
TIME_CHECK_EVERY = 4096


@dataclass(slots=True)
class TypeStat:
    label: str
    objects: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.label, "objects": self.objects, "bytes": self.bytes}


@dataclass(slots=True)
class PluginCensus:
    name: str
    objects: int = 0
    bytes: int = 0
    types: dict[str, TypeStat] = field(default_factory=dict)

    def top_types(self, limit: int = 8) -> list[dict[str, Any]]:
        ordered = sorted(self.types.values(), key=lambda item: item.bytes, reverse=True)
        return [item.to_dict() for item in ordered[: max(1, limit)]]

    def to_dict(self, top_types: int = 8) -> dict[str, Any]:
        return {
            "name": self.name,
            "objects": self.objects,
            "bytes": self.bytes,
            "types": self.top_types(top_types),
            "type_count": len(self.types),
        }


@dataclass(slots=True)
class CensusResult:
    generated_at: float = 0.0
    elapsed_ms: float = 0.0
    sample_rate: int = 1
    scanned: int = 0
    total_objects: int = 0
    plugins: dict[str, PluginCensus] = field(default_factory=dict)
    buckets: dict[str, TypeStat] = field(default_factory=dict)
    truncated: bool = False

    @property
    def scaled(self) -> bool:
        return self.sample_rate > 1

    @property
    def plugin_bytes(self) -> int:
        return sum(item.bytes for item in self.plugins.values())

    @property
    def plugin_objects(self) -> int:
        return sum(item.objects for item in self.plugins.values())

    def plugin_map(self) -> dict[str, int]:
        return {name: item.bytes for name, item in self.plugins.items() if item.bytes}

    def bucket_rows(self, limit: int = 14) -> list[dict[str, Any]]:
        ordered = sorted(self.buckets.values(), key=lambda item: item.bytes, reverse=True)
        head = ordered[: max(1, limit)]
        rest = ordered[len(head) :]
        rows = [
            {"bucket": item.label, "bytes": item.bytes, "objects": item.objects}
            for item in head
        ]
        if rest:
            rows.append(
                {
                    "bucket": "…",
                    "bytes": sum(item.bytes for item in rest),
                    "objects": sum(item.objects for item in rest),
                    "aggregated": len(rest),
                },
            )
        return rows

    def meta(self) -> dict[str, Any]:
        return {
            "method": "gc_objects_by_type_module",
            "generated_at": self.generated_at or None,
            "elapsed_ms": self.elapsed_ms,
            "sample_rate": self.sample_rate,
            "scaled": self.scaled,
            "scanned": self.scanned,
            "total_objects": self.total_objects,
            "truncated": self.truncated,
            "plugin_bytes": self.plugin_bytes,
            "plugin_objects": self.plugin_objects,
            "plugin_count": len(self.plugins),
        }


def build_module_index(registry: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Map ``module name -> plugin`` and ``module name -> non-plugin bucket``."""

    owners: dict[str, str] = {}
    buckets: dict[str, str] = {}
    builtin_names = frozenset(sys.builtin_module_names)
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        filename = getattr(module, "__file__", None)
        if not filename:
            if name == "builtins":
                buckets[name] = BUCKET_BUILTINS
            else:
                buckets[name] = BUCKET_STDLIB if name in builtin_names else BUCKET_UNKNOWN
            continue
        owner = registry.resolve_path(filename)
        if owner is not None:
            owners[name] = owner
        else:
            buckets[name] = registry.classify_path(filename)
    buckets.setdefault("builtins", BUCKET_BUILTINS)
    return owners, buckets


def run_census(
    registry: Any,
    sample_rate: int = 1,
    time_budget_ms: int = 4000,
    top_types: int = 8,
) -> CensusResult:
    """Walk the GC heap once and bucket every tracked object by owning module."""

    step = max(1, int(sample_rate))
    owners, buckets = build_module_index(registry)
    started = time.perf_counter()
    deadline = started + max(50, int(time_budget_ms)) / 1000.0
    result = CensusResult(sample_rate=step)

    objects = gc.get_objects()
    total = len(objects)
    result.total_objects = total
    getsizeof = sys.getsizeof
    plugins = result.plugins
    bucket_stats = result.buckets
    scanned = 0
    index = 0
    objects_id = id(objects)
    try:
        while index < total:
            obj = objects[index]
            index += step
            if id(obj) == objects_id:
                continue
            scanned += 1
            kind = type(obj)
            module_name = getattr(kind, "__module__", None)
            if not isinstance(module_name, str):
                module_name = BUCKET_UNKNOWN
            try:
                size = getsizeof(obj)
            except Exception:  # noqa: BLE001 - exotic __sizeof__ implementations
                size = 0
            owner = owners.get(module_name)
            if owner is not None:
                entry = plugins.get(owner)
                if entry is None:
                    entry = PluginCensus(name=owner)
                    plugins[owner] = entry
                entry.objects += 1
                entry.bytes += size
                label = module_name + "." + getattr(kind, "__qualname__", "?")
                stat = entry.types.get(label)
                if stat is None:
                    stat = TypeStat(label=label)
                    entry.types[label] = stat
                stat.objects += 1
                stat.bytes += size
            else:
                bucket = buckets.get(module_name)
                if bucket is None:
                    bucket = BUCKET_BUILTINS if module_name == "builtins" else BUCKET_UNKNOWN
                stat = bucket_stats.get(bucket)
                if stat is None:
                    stat = TypeStat(label=bucket)
                    bucket_stats[bucket] = stat
                stat.objects += 1
                stat.bytes += size
            if scanned % TIME_CHECK_EVERY == 0 and time.perf_counter() > deadline:
                result.truncated = True
                break
    finally:
        # Drop the giant list before doing anything else, it is the single
        # biggest transient allocation this plugin ever makes.
        del objects

    if step > 1:
        _scale(result, step)
    result.scanned = scanned
    result.elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    result.generated_at = time.time()
    for entry in plugins.values():
        entry.types = dict(
            sorted(
                entry.types.items(),
                key=lambda item: item[1].bytes,
                reverse=True,
            )[: max(1, top_types) * 3],
        )
    return result


def _scale(result: CensusResult, step: int) -> None:
    """Sampling inspects 1/N of the heap, so multiply the totals back up."""

    for entry in result.plugins.values():
        entry.objects *= step
        entry.bytes *= step
        for stat in entry.types.values():
            stat.objects *= step
            stat.bytes *= step
    for stat in result.buckets.values():
        stat.objects *= step
        stat.bytes *= step
