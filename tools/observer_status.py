#!/usr/bin/env python3
"""Print a compact status without exposing observer credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/memoryscope/observer.json")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())

    def read(endpoint):
        req = Request(
            f"http://127.0.0.1:{config['port']}/api/{endpoint}",
            headers={"Authorization": "Bearer " + config["token"]},
        )
        with urlopen(req, timeout=15) as response:
            return json.load(response)["data"]

    overview = read("overview")
    report = read("run")
    phases = report["phases"]
    print(
        json.dumps(
            {
                "overview": overview,
                "report": {
                    "phase_count": len(phases),
                    "plugin_count": len(report["plugins"]),
                    "probe_complete": report["probe_complete"],
                    "missing_events": report["missing_events"],
                    "unfinished_phases": report["unfinished_phases"],
                    "probe_overhead_ms": report["probe_overhead_ms"],
                    "phase_sources": sorted({p["source"] for p in phases}),
                    "top": sorted(
                        phases,
                        key=lambda p: p.get("process_rss_swap_delta") or 0,
                        reverse=True,
                    )[:10],
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
