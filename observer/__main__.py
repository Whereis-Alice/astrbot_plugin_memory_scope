"""CLI: python -m observer serve|launch|init-config|report."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import secrets
import signal
import threading
from pathlib import Path

from .config import Config


def main():
    parser = argparse.ArgumentParser(description="MemoryScope independent observer")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-config")
    init.add_argument("--config", required=True)
    init.add_argument("--state-dir", default="/var/lib/memoryscope")
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True)
    run = commands.add_parser("launch")
    run.add_argument("--config", required=True)
    run.add_argument("--no-probe", action="store_true")
    run.add_argument("script")
    run.add_argument("args", nargs=argparse.REMAINDER)
    report = commands.add_parser("report")
    report.add_argument("--config", required=True)
    report.add_argument("--run", default="")
    args = parser.parse_args()
    if args.command == "init-config":
        path = Path(args.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        config = Config(token=secrets.token_urlsafe(32), state_dir=args.state_dir)
        config.validate()
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump(dataclasses.asdict(config), stream, ensure_ascii=False, indent=2)
        print("Configuration created; token is stored only in the config file.")
    elif args.command == "launch":
        from .launch import launch

        launch(args.config, args.script, args.args, probe_enabled=not args.no_probe)
    elif args.command == "report":
        from .runtime import Observer

        observer = Observer(Config.load(args.config))
        try:
            runs = observer.store.runs()
            result = (
                observer.report(args.run or runs[0]["id"]) if runs else {"runs": []}
            )
            print(json.dumps(result, ensure_ascii=False))
        finally:
            observer.close()
    else:
        from .runtime import Observer
        from .server import make_server

        observer = Observer(Config.load(args.config))
        observer.start()
        server = make_server(observer)

        def stop(*_):
            server.experiments.cancel()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            worker = server.experiments.worker
            if worker:
                worker.join(timeout=65)
            server.server_close()
            observer.close()


if __name__ == "__main__":
    main()
