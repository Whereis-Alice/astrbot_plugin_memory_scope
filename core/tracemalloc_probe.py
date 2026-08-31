"""tracemalloc based per-plugin allocation attribution."""

from __future__ import annotations

import tracemalloc
from dataclasses import dataclass, field
from typing import Any

from .plugin_registry import PluginRegistry

DEFAULT_FRAMES = 12
MIN_FRAMES = 1
MAX_FRAMES = 40


@dataclass(slots=True)
class PluginAllocation:
    """Bytes currently allocated and attributed to one plugin."""

    attributed_bytes: int = 0
    direct_bytes: int = 0
    blocks: int = 0


@dataclass(slots=True)
class AllocationLine:
    filename: str
    lineno: int
    size: int
    blocks: int


@dataclass(slots=True)
class AttributionResult:
    total_bytes: int = 0
    traced_blocks: int = 0
    plugins: dict[str, PluginAllocation] = field(default_factory=dict)
    others: dict[str, int] = field(default_factory=dict)
    lines: list[AllocationLine] = field(default_factory=list)


class TracemallocProbe:
    """Thin wrapper around :mod:`tracemalloc` with plugin attribution.

    tracemalloc only knows about allocations made *after* it was started, so the
    probe records whether it had to start tracing itself.  In that case the
    numbers exclude everything the plugins allocated while importing, which the
    UI has to say out loud instead of pretending the totals are complete.
    """

    def __init__(self, frames: int = DEFAULT_FRAMES) -> None:
        self.frames = max(MIN_FRAMES, min(MAX_FRAMES, int(frames)))
        self.started_by_plugin = False
        self.was_tracing_at_load = tracemalloc.is_tracing()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def is_tracing() -> bool:
        return tracemalloc.is_tracing()

    @property
    def effective_frames(self) -> int:
        if tracemalloc.is_tracing():
            return tracemalloc.get_traceback_limit()
        return self.frames

    def start(self) -> bool:
        """Start tracing if needed.  Returns True when tracing is active."""

        if tracemalloc.is_tracing():
            return True
        tracemalloc.start(self.frames)
        self.started_by_plugin = True
        return True

    def stop(self, only_if_started_by_plugin: bool = True) -> None:
        if not tracemalloc.is_tracing():
            return
        if only_if_started_by_plugin and not self.started_by_plugin:
            return
        tracemalloc.stop()
        self.started_by_plugin = False

    def reset_peak(self) -> None:
        if tracemalloc.is_tracing():
            tracemalloc.reset_peak()

    def traced_memory(self) -> tuple[int, int]:
        if not tracemalloc.is_tracing():
            return (0, 0)
        return tracemalloc.get_traced_memory()

    def snapshot(self) -> tracemalloc.Snapshot | None:
        if not tracemalloc.is_tracing():
            return None
        return tracemalloc.take_snapshot()

    def status(self) -> dict[str, Any]:
        current, peak = self.traced_memory()
        return {
            "tracing": tracemalloc.is_tracing(),
            "frames": self.effective_frames,
            "configured_frames": self.frames,
            "current_bytes": current,
            "peak_bytes": peak,
            "started_by_plugin": self.started_by_plugin,
            "covers_plugin_import": self.was_tracing_at_load,
        }

    # ------------------------------------------------------------------
    # attribution
    # ------------------------------------------------------------------
    def analyze(
        self,
        snapshot: tracemalloc.Snapshot,
        registry: PluginRegistry,
        line_detail_for: str | None = None,
        line_limit: int = 25,
    ) -> AttributionResult:
        """Attribute every traced block to the innermost plugin frame.

        ``tracemalloc.Traceback`` is ordered from the oldest to the most recent
        frame, so walking it backwards means walking from the allocation site
        outwards.  The first frame that lives inside a plugin directory owns the
        block: that way ``json.loads`` called by a plugin is billed to the
        plugin instead of to the standard library.
        """

        result = AttributionResult()
        line_totals: dict[tuple[str, int], list[int]] = {}

        for stat in snapshot.statistics("traceback"):
            size = stat.size
            result.total_bytes += size
            result.traced_blocks += stat.count
            frames = stat.traceback
            frame_count = len(frames)
            owner: str | None = None
            owner_frame: Any = None
            is_direct = False
            for index in range(frame_count - 1, -1, -1):
                frame = frames[index]
                name = registry.resolve_path(frame.filename)
                if name is not None:
                    owner = name
                    owner_frame = frame
                    is_direct = index == frame_count - 1
                    break

            if owner is None:
                innermost = frames[frame_count - 1] if frame_count else None
                bucket = registry.classify_path(
                    getattr(innermost, "filename", None),
                )
                result.others[bucket] = result.others.get(bucket, 0) + size
                continue

            entry = result.plugins.get(owner)
            if entry is None:
                entry = PluginAllocation()
                result.plugins[owner] = entry
            entry.attributed_bytes += size
            entry.blocks += stat.count
            if is_direct:
                entry.direct_bytes += size

            if line_detail_for is not None and owner == line_detail_for:
                key = (owner_frame.filename, owner_frame.lineno)
                bucket_line = line_totals.get(key)
                if bucket_line is None:
                    line_totals[key] = [size, stat.count]
                else:
                    bucket_line[0] += size
                    bucket_line[1] += stat.count

        if line_totals:
            ordered = sorted(
                line_totals.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )[: max(1, line_limit)]
            result.lines = [
                AllocationLine(
                    filename=key[0],
                    lineno=key[1],
                    size=value[0],
                    blocks=value[1],
                )
                for key, value in ordered
            ]

        return result