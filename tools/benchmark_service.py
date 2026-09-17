#!/usr/bin/env python3
"""Explicit repeated startup measurement. This command RESTARTS the configured bot."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--settle", type=float, default=20)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm-restart", action="store_true", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    records = []
    for index in range(args.runs):
        t0, cpu0 = time.monotonic(), time.process_time()
        requested = time.time()
        subprocess.run(["systemctl", "restart", config["unit"]], check=True, timeout=90)
        while time.monotonic() - t0 < config.get("ready_timeout", 240):
            try:
                with socket.create_connection(
                    (
                        config.get("ready_host", "127.0.0.1"),
                        config.get("ready_port", 6185),
                    ),
                    timeout=0.1,
                ):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("AstrBot readiness timeout")
        elapsed = time.monotonic() - t0
        time.sleep(args.settle)
        url = f"http://127.0.0.1:{config['port']}/api/overview"
        req = Request(url, headers={"Authorization": "Bearer " + config["token"]})
        with urlopen(req, timeout=10) as response:
            overview = json.load(response)["data"]
        run = overview.get("run") or {}
        record = {
            "label": args.label,
            "iteration": index + 1,
            "requested_at": requested,
            "restart_to_ready_seconds": elapsed,
            "run_id": run.get("id"),
            "run": run,
            "latest": overview.get("latest"),
            "observer": overview.get("observer"),
            "measurement_cpu_seconds": time.process_time() - cpu0,
        }
        records.append(record)
        Path(args.output).write_text(json.dumps(records, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "label": args.label,
                    "iteration": index + 1,
                    "seconds": round(elapsed, 3),
                    "probe_ms": run.get("probe_overhead_ms"),
                    "run_id": run.get("id"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
