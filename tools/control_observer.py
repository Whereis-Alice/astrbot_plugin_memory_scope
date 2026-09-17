#!/usr/bin/env python3
"""Local admin CLI for observer experiment configuration and API access."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/memoryscope/observer.json")
    sub = parser.add_subparsers(dest="action", required=True)
    enable = sub.add_parser("enable-experiments")
    enable.add_argument("--settle", type=float, default=60)
    enable.add_argument("--observe", type=float, default=30)
    sub.add_parser("status")
    start = sub.add_parser("start")
    start.add_argument("--plugin", required=True)
    start.add_argument("--mode", choices=["skip", "disable"], default="skip")
    start.add_argument("--rounds", type=int, default=1)
    start.add_argument("--confirm-restart", action="store_true", required=True)
    args = parser.parse_args()
    path = Path(args.config)
    config = json.loads(path.read_text())
    if args.action == "enable-experiments":
        config.update(
            allow_experiments=True,
            settle_seconds=args.settle,
            observe_seconds=args.observe,
        )
        temp = path.with_suffix(".tmp")
        with temp.open("w") as stream:
            os.chmod(temp, 0o600)
            json.dump(config, stream, indent=2)
        os.replace(temp, path)
        print("Experiment configuration updated. Restart memoryscope.service to apply.")
        return
    body = None
    if args.action == "start":
        body = json.dumps(
            {
                "plugin": args.plugin,
                "mode": args.mode,
                "rounds": args.rounds,
                "confirm_restart": True,
            }
        ).encode()
    req = Request(
        f"http://127.0.0.1:{config['port']}/api/experiments",
        data=body,
        headers={
            "Authorization": "Bearer " + config["token"],
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=15) as response:
        print(json.dumps(json.load(response), ensure_ascii=False))


if __name__ == "__main__":
    main()
