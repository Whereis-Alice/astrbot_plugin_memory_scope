"""Evidence-labelled startup windows and controlled run comparisons."""

from __future__ import annotations

import bisect
import statistics
from itertools import pairwise


def startup_report(run: dict, samples: list[dict], events: list[dict]) -> dict:
    times = [s["ts"] for s in samples]
    starts, phases, packages = {}, [], {}
    sequences = set()

    def sample_at(ts):
        if not times:
            return None
        index = bisect.bisect_left(times, ts)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(times)]
        i = min(candidates, key=lambda j: abs(times[j] - ts))
        return samples[i] if abs(times[i] - ts) <= 0.25 else None

    for event in events:
        if "seq" in event:
            sequences.add(event["seq"])
        if event["kind"] == "phase_start":
            starts[event["seq"]] = event
        elif event["kind"] == "phase_end":
            before = starts.pop(event.get("span"), None)
            if not before:
                continue
            a, b = sample_at(before["ts"]), sample_at(event["ts"])
            baseline = (a or {}).get("memory", {}).get("anon_swap")
            finish = (b or {}).get("memory", {}).get("anon_swap")
            # Multiple boundaries inside the same sampling interval cannot be resolved.
            resolved = a is not None and b is not None and a["ts"] != b["ts"]
            points = [
                s["memory"]["anon_swap"]
                for s in samples[
                    bisect.bisect_left(times, before["ts"]) : bisect.bisect_right(
                        times, event["ts"]
                    )
                ]
                if s.get("memory", {}).get("anon_swap") is not None
            ]
            counters0, counters1 = before.get("counters", {}), event.get("counters", {})
            inline = None
            if all(k in c for c in (counters0, counters1) for k in ("VmRSS", "VmSwap")):
                inline = sum(counters1.values()) - sum(counters0.values())
            phases.append(
                {
                    "plugin": event["plugin"],
                    "phase": event["phase"],
                    "start": before["ts"],
                    "end": event["ts"],
                    "duration_ms": (
                        event.get("monotonic_ns", 0) - before.get("monotonic_ns", 0)
                    )
                    / 1e6,
                    "process_rss_swap_delta": inline,
                    "service_anon_swap_delta": finish - baseline
                    if resolved and finish is not None and baseline is not None
                    else None,
                    "service_peak_above_start": max(points) - baseline
                    if resolved and points and baseline is not None
                    else None,
                    "failed": event.get("failed", False),
                    "source": "startup_probe",
                    "attribution": "interval_estimate",
                }
            )
            for package in event.get("new_packages", []):
                packages.setdefault(
                    package,
                    {"name": package, "first_importer": event["plugin"], "bytes": None},
                )
    finished = any(e["kind"] == "probe_finished" for e in events)
    measured = {p["plugin"] for p in phases}
    markers = [e for e in events if e["kind"] == "log_plugin_start"]
    for before, after in pairwise(markers):
        if before["plugin"] in measured:
            continue
        a, b = sample_at(before["ts"]), sample_at(after["ts"])
        av = (a or {}).get("memory", {}).get("anon_swap")
        bv = (b or {}).get("memory", {}).get("anon_swap")
        phases.append(
            {
                "plugin": before["plugin"],
                "phase": "startup_window",
                "start": before["ts"],
                "end": after["ts"],
                "duration_ms": (after["ts"] - before["ts"]) * 1000,
                "process_rss_swap_delta": None,
                "service_anon_swap_delta": bv - av
                if a is not b and av is not None and bv is not None
                else None,
                "service_peak_above_start": None,
                "failed": False,
                "source": "journal_receipt_time",
                "attribution": "coarse_interval_estimate",
            }
        )
    gaps = max(sequences) - len(sequences) if sequences else None
    plugins = {}
    for phase in phases:
        row = plugins.setdefault(
            phase["plugin"], {"plugin": phase["plugin"], "phases": [], "duration_ms": 0}
        )
        row["phases"].append(phase)
        row["duration_ms"] += phase["duration_ms"]
    for item in run.get("inventory", []):
        key = item.get("root_dir_name") or item.get("import_key") or item.get("name")
        row = plugins.setdefault(
            key, {"plugin": key, "phases": [], "duration_ms": None}
        )
        row.update(
            display_name=item.get("display_name"),
            activated=item.get("activated"),
            version=item.get("version"),
        )
    return {
        "run": run,
        "samples": samples,
        "phases": phases,
        "plugins": list(plugins.values()),
        "packages": list(packages.values()),
        "child_spawns": [e for e in events if e["kind"] == "child_spawn"],
        "probe_complete": finished
        and gaps == 0
        and not starts
        and not any(e["kind"] == "probe_error" for e in events),
        "missing_events": gaps,
        "unfinished_phases": len(starts),
        "probe_overhead_ms": max(
            (e.get("overhead_ms", 0) for e in events), default=None
        ),
        "notes": [
            "时间段增量含其他并发任务；不是插件独占内存。",
            "共享依赖不重复分摊；未测到的值为 null，不能当成 0。",
            "RSS+Swap 与 cgroup anon+swap 口径不同，不能相加。",
        ],
    }


def summarize_run(
    run: dict, samples: list[dict], settle: float, observe: float
) -> dict:
    ready = run.get("ready_at")
    window = [
        s
        for s in samples
        if ready is not None and ready + settle <= s["ts"] <= ready + settle + observe
    ]
    values = [
        s["memory"]["anon_swap"]
        for s in window
        if s.get("memory", {}).get("anon_swap") is not None
    ]
    peak = [
        s["memory"]["anon_swap"]
        for s in samples
        if ready is not None
        and s["ts"] <= ready
        and s.get("memory", {}).get("anon_swap") is not None
    ]
    complete = (
        len(values) >= 3
        and bool(window)
        and window[-1]["ts"] - window[0]["ts"] >= observe * 0.7
    )
    return {
        "run_id": run["id"],
        "median": statistics.median(values) if complete else None,
        "range": [min(values), max(values)] if values else None,
        "samples": len(values),
        "complete": complete,
        "startup_peak": max(peak) if peak else None,
        "ready_seconds": ready - run["started_at"] if ready else None,
        "fingerprint": run.get("fingerprint"),
        "window": {"settle": settle, "observe": observe},
    }


def compare(groups: dict[str, list[dict]]) -> dict:
    if set(groups) != {"A", "B"} or not groups["A"] or not groups["B"]:
        raise ValueError("Both A and B runs are required")
    all_runs = groups["A"] + groups["B"]
    if len({r["run_id"] for r in all_runs}) != len(all_runs):
        raise ValueError("A run cannot be compared with itself or counted twice")
    fingerprints = {r.get("fingerprint") for r in all_runs}
    valid = all(r.get("complete") and r.get("median") is not None for r in all_runs)
    comparable = len(fingerprints) == 1 and None not in fingerprints
    result = {
        "groups": groups,
        "valid_windows": valid,
        "matching_environment": comparable,
        "saving_bytes": None,
        "classification": "insufficient_data",
        "notes": [
            "结果是当前插件组合下的条件收益；多个插件的节省量不能直接相加。",
            "消息流量及外部服务不由此测试控制；正式结论需要一致工作负载。",
        ],
    }
    if valid:
        a, b = ([r["median"] for r in groups[name]] for name in ("A", "B"))
        saving = statistics.median(a) - statistics.median(b)
        spread = max(
            max(a) - min(a),
            max(b) - min(b),
            max(r["range"][1] - r["range"][0] for r in all_runs),
        )
        result.update(
            saving_bytes=saving,
            variability_bytes=spread,
            classification="environment_changed"
            if not comparable
            else "single_trial"
            if min(len(a), len(b)) < 2
            else "within_variation"
            if abs(saving) <= spread
            else "difference_observed",
        )
    return result
