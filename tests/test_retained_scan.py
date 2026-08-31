"""Reference-graph scan: exclusive vs shared, boundaries, denylist, limits."""

from __future__ import annotations

import sys
import types

from core import retained_scan
from core.retained_scan import ScanLimits, build_denylist, is_boundary, safe_sizeof, scan


class Holder:
    def __init__(self, payload, shared=None):
        self.payload = payload
        self.shared = shared


def make_pair(shared_blob_size=20_000, own_blob_size=30_000):
    shared = {"blob": bytearray(shared_blob_size)}
    left = Holder([bytearray(own_blob_size)], shared)
    right = Holder([bytearray(own_blob_size)], shared)
    return left, right, shared


def test_exclusive_and_shared_are_split():
    left, right, shared = make_pair()

    report = scan({"left": [left], "right": [right]}, set())

    for name in ("left", "right"):
        result = report.results[name]
        assert result.exclusive_bytes >= 30_000
        assert result.exclusive_objects > 0
        # The shared dict, its bytearray and the Holder class are reachable
        # from both plugins and must not be counted as exclusive.
        assert result.shared_bytes >= safe_sizeof(shared) + safe_sizeof(shared["blob"])
        assert result.shared_objects >= 2
        assert result.total_bytes == result.exclusive_bytes + result.shared_bytes
        assert result.truncated is False

    assert report.scanned_objects > 0
    assert report.truncated is False
    assert report.elapsed_ms >= 0.0


def test_single_plugin_has_no_shared_bytes():
    left, _right, _shared = make_pair()

    result = scan({"left": [left]}, set()).results["left"]

    assert result.shared_bytes == 0
    assert result.shared_objects == 0
    assert result.exclusive_bytes >= 50_000


def test_denylisted_objects_are_skipped():
    left, right, shared = make_pair()

    full = scan({"left": [left], "right": [right]}, set()).results["left"].total_bytes
    denied = {id(shared), id(shared["blob"])}
    pruned = scan({"left": [left], "right": [right]}, denied).results["left"].total_bytes

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
        "total_bytes",
        "truncated",
    }
    assert payload["total_bytes"] == payload["exclusive_bytes"] + payload["shared_bytes"]


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
