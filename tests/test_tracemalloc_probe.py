"""Attribution rules: innermost plugin frame owns the block."""

from __future__ import annotations

import tracemalloc
from dataclasses import dataclass

import pytest

from core.tracemalloc_probe import DEFAULT_FRAMES, TracemallocProbe


@dataclass
class FakeFrame:
    filename: str
    lineno: int


@dataclass
class FakeStat:
    size: int
    count: int
    traceback: list


class FakeSnapshot:
    def __init__(self, stats):
        self._stats = stats

    def statistics(self, key_type, cumulative=False):
        assert key_type == "traceback"
        return list(self._stats)


class StubRegistry:
    """Minimal stand-in: explicit file -> plugin and file -> bucket mapping."""

    def __init__(self, owners=None, buckets=None):
        self.owners = owners or {}
        self.buckets = buckets or {}

    def resolve_path(self, filename):
        return self.owners.get(filename)

    def classify_path(self, filename):
        return self.buckets.get(filename, "unknown")


# Traceback order is oldest frame first, allocation site last.
CORE = "/astrbot/core/star/star_handler.py"
PLUGIN_MAIN = "/data/plugins/demo/main.py"
PLUGIN_UTIL = "/data/plugins/demo/util.py"
STDLIB_JSON = "/usr/lib/python3/json/decoder.py"
OTHER_PLUGIN = "/data/plugins/other/main.py"

REGISTRY = StubRegistry(
    owners={PLUGIN_MAIN: "demo", PLUGIN_UTIL: "demo", OTHER_PLUGIN: "other"},
    buckets={CORE: "astrbot_core", STDLIB_JSON: "python_stdlib"},
)


def analyze(stats, **kwargs):
    probe = TracemallocProbe()
    return probe.analyze(FakeSnapshot(stats), REGISTRY, **kwargs)


def test_allocation_inside_plugin_is_direct():
    result = analyze(
        [
            FakeStat(
                size=4096,
                count=3,
                traceback=[FakeFrame(CORE, 10), FakeFrame(PLUGIN_MAIN, 42)],
            ),
        ],
    )

    assert result.total_bytes == 4096
    assert result.traced_blocks == 3
    assert result.plugins["demo"].attributed_bytes == 4096
    assert result.plugins["demo"].direct_bytes == 4096
    assert result.plugins["demo"].blocks == 3
    assert result.others == {}


def test_stdlib_allocation_is_billed_to_the_calling_plugin():
    """json.loads called by a plugin must not land in the stdlib bucket."""

    result = analyze(
        [
            FakeStat(
                size=1024,
                count=1,
                traceback=[
                    FakeFrame(CORE, 10),
                    FakeFrame(PLUGIN_MAIN, 42),
                    FakeFrame(STDLIB_JSON, 353),
                ],
            ),
        ],
    )

    allocation = result.plugins["demo"]
    assert allocation.attributed_bytes == 1024
    # The allocation site itself is not plugin code, so it is not "direct".
    assert allocation.direct_bytes == 0
    assert result.others == {}


def test_innermost_plugin_frame_wins_between_two_plugins():
    result = analyze(
        [
            FakeStat(
                size=2048,
                count=2,
                traceback=[
                    FakeFrame(OTHER_PLUGIN, 5),
                    FakeFrame(PLUGIN_UTIL, 77),
                    FakeFrame(STDLIB_JSON, 353),
                ],
            ),
        ],
    )

    assert set(result.plugins) == {"demo"}
    assert result.plugins["demo"].attributed_bytes == 2048


def test_blocks_without_plugin_frames_go_to_buckets():
    result = analyze(
        [
            FakeStat(size=512, count=1, traceback=[FakeFrame(CORE, 10)]),
            FakeStat(size=256, count=1, traceback=[FakeFrame(CORE, 20)]),
            FakeStat(
                size=128,
                count=1,
                traceback=[FakeFrame(CORE, 10), FakeFrame(STDLIB_JSON, 353)],
            ),
            FakeStat(size=64, count=1, traceback=[FakeFrame("/unknown/x.py", 1)]),
        ],
    )

    assert result.plugins == {}
    assert result.others == {"astrbot_core": 768, "python_stdlib": 128, "unknown": 64}
    assert result.total_bytes == 960


def test_empty_traceback_is_not_fatal():
    result = analyze([FakeStat(size=32, count=1, traceback=[])])

    assert result.total_bytes == 32
    assert result.others == {"unknown": 32}


def test_line_detail_aggregates_and_sorts_by_size():
    result = analyze(
        [
            FakeStat(size=100, count=1, traceback=[FakeFrame(PLUGIN_MAIN, 10)]),
            FakeStat(size=300, count=2, traceback=[FakeFrame(PLUGIN_MAIN, 10)]),
            FakeStat(size=900, count=1, traceback=[FakeFrame(PLUGIN_UTIL, 20)]),
            FakeStat(size=700, count=1, traceback=[FakeFrame(OTHER_PLUGIN, 30)]),
        ],
        line_detail_for="demo",
    )

    assert [(line.filename, line.lineno, line.size, line.blocks) for line in result.lines] == [
        (PLUGIN_UTIL, 20, 900, 1),
        (PLUGIN_MAIN, 10, 400, 3),
    ]


def test_line_detail_respects_the_limit():
    stats = [
        FakeStat(size=size, count=1, traceback=[FakeFrame(PLUGIN_MAIN, size)])
        for size in (10, 20, 30, 40)
    ]
    result = analyze(stats, line_detail_for="demo", line_limit=2)

    assert [line.size for line in result.lines] == [40, 30]


def test_no_line_detail_requested():
    result = analyze([FakeStat(size=10, count=1, traceback=[FakeFrame(PLUGIN_MAIN, 1)])])
    assert result.lines == []


def test_frames_are_clamped():
    assert TracemallocProbe(0).frames == 1
    assert TracemallocProbe(999).frames == 40
    assert TracemallocProbe().frames == DEFAULT_FRAMES


def test_status_without_tracing_is_zeroed():
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already started by the environment")
    probe = TracemallocProbe(8)
    status = probe.status()

    assert status["tracing"] is False
    assert status["current_bytes"] == 0
    assert status["peak_bytes"] == 0
    assert status["frames"] == 8
    assert probe.snapshot() is None
    assert probe.traced_memory() == (0, 0)
    # Stopping while not tracing must be a no-op instead of an error.
    probe.stop()


def test_lifecycle_and_real_attribution():
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already started by the environment")

    probe = TracemallocProbe(6)
    try:
        assert probe.start() is True
        assert probe.started_by_plugin is True
        # An externally started session would not be marked as ours.
        assert probe.status()["covers_plugin_import"] is False

        payload = [bytearray(4096) for _ in range(64)]
        snapshot = probe.snapshot()
        assert snapshot is not None

        registry = StubRegistry(owners={__file__: "self_test"})
        result = probe.analyze(snapshot, registry)

        allocation = result.plugins.get("self_test")
        assert allocation is not None
        assert allocation.attributed_bytes >= 64 * 4096
        assert allocation.direct_bytes > 0
        assert result.total_bytes >= allocation.attributed_bytes
        assert len(payload) == 64

        current, peak = probe.traced_memory()
        assert current > 0 and peak >= current
        probe.reset_peak()
        assert probe.status()["peak_bytes"] <= peak
    finally:
        probe.stop()

    assert tracemalloc.is_tracing() is False
    assert probe.started_by_plugin is False


def test_stop_keeps_a_session_the_plugin_did_not_start():
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already started by the environment")

    tracemalloc.start(4)
    try:
        probe = TracemallocProbe(4)
        assert probe.was_tracing_at_load is True
        assert probe.start() is True
        assert probe.started_by_plugin is False

        probe.stop()  # only_if_started_by_plugin defaults to True
        assert tracemalloc.is_tracing() is True

        probe.stop(only_if_started_by_plugin=False)
        assert tracemalloc.is_tracing() is False
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
