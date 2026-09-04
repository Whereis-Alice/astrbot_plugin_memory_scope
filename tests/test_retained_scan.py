"""Reference-graph scan: exclusive vs shared, boundaries, denylist, fair quotas."""

from __future__ import annotations

import sys
import types

import pytest

from core import retained_scan
from core.retained_scan import (
    RetainedScanner,
    ScanLimits,
    build_denylist,
    is_boundary,
    safe_sizeof,
    scan,
)


class Holder:
    def __init__(self, payload, shared=None):
        self.payload = payload
        self.shared = shared


def make_pair(shared_blob_size=20_000, own_blob_size=30_000):
    shared = {"blob": bytearray(shared_blob_size)}
    left = Holder([bytearray(own_blob_size)], shared)
    right = Holder([bytearray(own_blob_size)], shared)
    return left, right, shared


def make_fat(count):
    """A plugin holding many small objects, so it burns object budget."""

    return Holder([{"i": index, "pad": bytearray(64)} for index in range(count)])


def test_exclusive_and_shared_are_split():
    left, right, shared = make_pair()

    report = scan({"left": [left], "right": [right]}, set())

    shared_total = safe_sizeof(shared) + safe_sizeof(shared["blob"])
    for name in ("left", "right"):
        result = report.results[name]
        assert result.exclusive_bytes >= 30_000
        assert result.exclusive_objects > 0
        # The shared dict, its bytearray and the Holder class are reachable
        # from both plugins and must not be counted as exclusive.
        assert result.shared_full_bytes >= shared_total
        assert result.shared_objects >= 2
        # Shared bytes are split 1/N so two plugins never double count them.
        assert result.shared_bytes == pytest.approx(result.shared_full_bytes / 2)
        assert result.total_bytes == result.exclusive_bytes + result.shared_share_bytes
        assert result.truncated is False

    left_result = report.results["left"]
    right_result = report.results["right"]
    assert (
        left_result.shared_share_bytes + right_result.shared_share_bytes
        <= left_result.shared_full_bytes + 1
    )

    assert report.scanned_objects > 0
    assert report.truncated is False
    assert report.elapsed_ms >= 0.0
    assert report.plugin_count == 2
    assert report.complete_count == 2


def test_single_plugin_has_no_shared_bytes():
    left, _right, _shared = make_pair()

    result = scan({"left": [left]}, set()).results["left"]

    assert result.shared_bytes == 0
    assert result.shared_objects == 0
    assert result.exclusive_bytes >= 50_000


def test_denylisted_objects_are_skipped():
    left, _right, shared = make_pair()

    full = scan({"left": [left]}, set()).results["left"].total_bytes
    denied = {id(shared), id(shared["blob"])}
    pruned = scan({"left": [left]}, denied).results["left"].total_bytes

    assert full - pruned >= 20_000


def test_boundary_types_are_sized_but_not_traversed():
    module = types.ModuleType("memoryscope_fake_module")
    module.big = bytearray(200_000)
    holder = Holder([module])

    result = scan({"left": [holder]}, set()).results["left"]

    assert result.total_bytes < 100_000
    assert is_boundary(module) is True
    assert is_boundary(holder) is False


def test_missing_roots_yield_zeroed_results():
    report = scan({"left": [None], "right": []}, set())

    assert report.results["left"].total_bytes == 0
    assert report.results["right"].total_bytes == 0
    assert report.scanned_objects == 0


def test_per_plugin_object_limit_marks_truncation():
    left, right, _shared = make_pair()

    report = scan(
        {"left": [left], "right": [right]},
        set(),
        ScanLimits(max_objects_per_plugin=1, max_objects_total=10_000),
    )

    assert report.truncated is True
    assert report.results["left"].truncated is True
    assert report.results["right"].truncated is True


def test_max_depth_stops_the_walk():
    deep = bytearray(40_000)
    for _ in range(5):
        deep = [deep]
    holder = Holder(deep)

    shallow = scan({"left": [holder]}, set(), ScanLimits(max_depth=1)).results["left"]
    full = scan({"left": [holder]}, set(), ScanLimits()).results["left"]

    assert shallow.total_bytes < full.total_bytes
    assert full.total_bytes >= 40_000


def test_result_to_dict_shape():
    left, right, _shared = make_pair()
    payload = scan({"left": [left], "right": [right]}, set()).results["left"].to_dict()

    assert set(payload) == {
        "exclusive_bytes",
        "exclusive_objects",
        "shared_bytes",
        "shared_objects",
        "shared_full_bytes",
        "total_bytes",
        "truncated",
        "scanned_objects",
    }
    assert payload["total_bytes"] == payload["exclusive_bytes"] + payload["shared_bytes"]
    assert payload["shared_full_bytes"] >= payload["shared_bytes"]
    assert payload["scanned_objects"] > 0


def test_round_robin_rotates_the_starting_plugin():
    scanner = RetainedScanner()
    roots = {"a": [Holder([1])], "b": [Holder([2])], "c": [Holder([3])]}

    first = scanner.scan(roots, set())
    second = scanner.scan(roots, set())
    third = scanner.scan(roots, set())
    fourth = scanner.scan(roots, set())

    assert [first.start_index, second.start_index] == [0, 1]
    assert [third.start_index, fourth.start_index] == [2, 0]
    assert scanner.rounds == 4


def test_a_fat_plugin_no_longer_starves_the_rest():
    roots = {
        "fat": [make_fat(4_000)],
        "small_a": [Holder([bytearray(10_000)])],
        "small_b": [Holder([bytearray(10_000)])],
        "small_c": [Holder([bytearray(10_000)])],
    }

    report = RetainedScanner().scan(
        roots,
        set(),
        ScanLimits(max_objects_total=4_000, max_objects_per_plugin=4_000),
    )

    assert report.results["fat"].truncated is True
    for name in ("small_a", "small_b", "small_c"):
        result = report.results[name]
        assert result.truncated is False
        assert result.total_bytes >= 10_000
    assert report.truncated_count == 1
    assert report.complete_count == 3


def test_every_plugin_gets_a_fair_object_allowance():
    roots = {name: [make_fat(3_000)] for name in ("a", "b", "c", "d")}

    report = RetainedScanner().scan(
        roots,
        set(),
        ScanLimits(max_objects_total=8_000, max_objects_per_plugin=8_000),
    )

    for name in roots:
        scanned = report.results[name].scanned_objects
        assert 0 < scanned <= 2_000
    assert report.scanned_objects <= 8_000


def test_duty_cycle_controls_the_sleep_between_slices():
    paced = ScanLimits(slice_ms=15, duty_percent=25)

    assert paced.slice_seconds == pytest.approx(0.015)
    # 25% duty: work 15 ms, then stay out of the way for 45 ms.
    assert paced.sleep_seconds == pytest.approx(0.045)

    assert ScanLimits(duty_percent=100).sleep_seconds == 0.0
    assert ScanLimits(time_budget_ms=3000).work_budget_seconds == pytest.approx(3.0)


def test_safe_sizeof_never_raises():
    class Hostile:
        def __sizeof__(self):
            raise RuntimeError("nope")

    assert safe_sizeof(Hostile()) == 0
    assert safe_sizeof(bytearray(1024)) >= 1024


def test_build_denylist_covers_context_graph_and_modules():
    nested = {"secret": [object()]}
    context = Holder([nested])

    denied = build_denylist(context, depth=3)

    assert id(context) in denied
    assert id(nested) in denied
    assert id(sys.modules) in denied


def test_build_denylist_extra_roots_are_shallow():
    child = object()
    parent = {"child": child}

    denied = build_denylist(None, extra_roots=[parent])

    assert id(parent) in denied
    assert id(child) not in denied


def test_denylist_blocks_core_objects_from_plugin_totals():
    core_blob = bytearray(80_000)
    context = Holder([core_blob])
    plugin = Holder([bytearray(10_000), core_blob])

    denied = build_denylist(context, depth=3)
    result = scan({"plugin": [plugin]}, denied).results["plugin"]

    assert result.total_bytes < 80_000
    assert retained_scan.BOUNDARY_TYPES
