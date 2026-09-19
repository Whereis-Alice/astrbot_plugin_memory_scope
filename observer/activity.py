"""Bounded activity evidence and interval comparisons, never causal accounting."""

from __future__ import annotations

import math
import re
from itertools import pairwise

CATEGORIES = {"handler", "llm", "tool", "lifecycle", "diagnostic", "trace", "process"}
STATUSES = {"running", "ok", "error", "cancelled", "expired"}


def number(value):
    try:
        return (
            value
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            else None
        )
    except OverflowError:
        return None


def label(value, limit=120):
    # Identifiers only: no messages, arguments, exception strings or URLs.
    return re.sub(r"[^\w.:/\- ]", "_", str(value or ""))[:limit]


def clean_record(value, now):
    if not isinstance(value, dict):
        raise ValueError("Invalid activity record")  # noqa: TRY004 - uniform API validation
    start, end = number(value.get("start")), number(value.get("end"))
    if start is None or not now - 86400 <= start <= now + 10:
        raise ValueError("Invalid activity start")
    status = value.get("status")
    if (
        not isinstance(value.get("category"), str)
        or not isinstance(status, str)
        or value["category"] not in CATEGORIES
        or status not in STATUSES
    ):
        raise ValueError("Invalid activity category or status")
    if status == "running":
        end = None
    elif end is None or not start <= end <= now + 10:
        raise ValueError("Invalid activity end")
    ident = value.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident):
        raise ValueError("Invalid activity id")
    result = {
        "id": ident,
        "start": start,
        "end": end,
        "category": value["category"],
        "plugin": label(value.get("plugin")),
        "operation": label(value.get("operation")),
        "status": status,
        "duration_ms": max(0, number(value.get("duration_ms")) or 0),
    }
    allocations = value.get("allocations")
    if isinstance(allocations, list):
        result["allocations"] = [
            {
                "file": label(x.get("file"), 180),
                "line": max(0, int(number(x.get("line")) or 0)),
                "bytes": int(number(x.get("bytes")) or 0),
                "count": int(number(x.get("count")) or 0),
            }
            for x in allocations[:20]
            if isinstance(x, dict)
        ]
    return result


def difference(a, b):
    return b - a if number(a) is not None and number(b) is not None else None


def growth_report(samples, run, since, until):
    """Use actual endpoint samples, with explicit gaps, identities and missing values."""
    if len(samples) < 2:
        return {
            "available": False,
            "reason": "insufficient_samples",
            "requested": [since, until],
        }
    samples = sorted(samples, key=lambda s: s["ts"])
    first, last = samples[0], samples[-1]
    a, b = first.get("memory", {}), last.get("memory", {})
    fields = ("current", "anon", "file", "kernel", "swap", "anon_swap")
    memory = {
        k: {
            "before": a.get(k),
            "after": b.get(k),
            "delta": difference(a.get(k), b.get(k)),
        }
        for k in fields
    }
    before = {(p["pid"], p["start_ticks"]): p for p in first.get("processes", [])}
    after = {(p["pid"], p["start_ticks"]): p for p in last.get("processes", [])}
    processes = []
    for key in before.keys() | after.keys():
        left, right = before.get(key, {}), after.get(key, {})
        processes.append(
            {
                "pid": key[0],
                "start_ticks": key[1],
                "name": (right or left).get("name"),
                "main": key == (run.get("pid"), run.get("start_ticks")),
                "state": "present"
                if left and right
                else "appeared"
                if right
                else "disappeared",
                "rss_before": left.get("rss"),
                "rss_after": right.get("rss"),
                "rss_delta": difference(left.get("rss"), right.get("rss")),
                "sampled_before": left.get("sampled_at"),
                "sampled_after": right.get("sampled_at"),
            }
        )
    values = [s.get("memory", {}).get("current") for s in samples]
    values = [x for x in values if number(x) is not None]
    gaps = [y["ts"] - x["ts"] for x, y in pairwise(samples)]
    return {
        "available": True,
        "requested": [since, until],
        "actual": [first["ts"], last["ts"]],
        "sample_count": len(samples),
        "max_gap_seconds": max(gaps, default=0),
        "peak": max(values) if values else None,
        "memory": memory,
        "processes": sorted(
            processes, key=lambda p: abs(p["rss_delta"] or 0), reverse=True
        ),
        "causal": False,
    }
