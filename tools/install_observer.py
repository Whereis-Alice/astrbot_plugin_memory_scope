#!/usr/bin/env python3
"""Install the observer independently. Generate a reversible AstrBot drop-in.

Never modifies AstrBot source, config or data. Does not restart AstrBot.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/opt/memoryscope")
    parser.add_argument("--config", default="/etc/memoryscope/observer.json")
    parser.add_argument("--state", default="/var/lib/memoryscope")
    parser.add_argument("--astrbot-root", default="/root/AstrBot")
    parser.add_argument("--astrbot-python", default="/root/AstrBot/.venv/bin/python")
    parser.add_argument("--unit", default="astrbot.service")
    parser.add_argument("--enable-probe", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Run as root on a Linux systemd host")
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    from observer.config import Config

    destination = Path(args.root).resolve()
    botroot = Path(args.astrbot_root).resolve()
    if destination == botroot or destination.is_relative_to(botroot):
        parser.error("Observer installation must be outside AstrBot")
    for value in (
        args.root,
        args.config,
        args.state,
        args.astrbot_root,
        args.astrbot_python,
    ):
        if any(c in value for c in '\n\r"%') or not Path(value).is_absolute():
            parser.error(
                "Use absolute deployment paths without quotes, percent signs or newlines"
            )
    config_file = Path(args.config)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if config_file.exists():
        config = Config.load(config_file)
    else:
        config = Config(
            token=secrets.token_urlsafe(32), state_dir=args.state, unit=args.unit
        )
        config.validate()
        with config_file.open("x") as stream:
            os.chmod(config_file, 0o600)
            json.dump(dataclasses.asdict(config), stream, indent=2)
    backup = Path("/root/memoryscope-backups") / datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True, mode=0o700)
    for path in (
        destination,
        Path("/etc/systemd/system/memoryscope.service"),
        Path("/etc/systemd/system") / (args.unit + ".d") / "90-memoryscope.conf",
    ):
        if path.exists():
            if path.is_dir():
                shutil.copytree(path, backup / path.name)
            else:
                shutil.copy2(path, backup / path.name)
    shutil.copy2(config_file, backup / "observer.json")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("observer", "pages"):
        shutil.copytree(
            source / name,
            destination / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    (destination / "entry.py").write_text(
        "from observer.__main__ import main\nmain()\n"
    )
    Path(config.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    service = f'''# Managed by MemoryScope installer
[Unit]
Description=MemoryScope independent AstrBot memory observer
After=network.target
Before={args.unit}

[Service]
Type=simple
ExecStart=/usr/bin/python3 "{destination}/entry.py" serve --config "{config_file}"
WorkingDirectory={destination}
Restart=on-failure
RestartSec=3
UMask=0077
Nice=10
MemoryMax=192M
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={config.state_dir}

[Install]
WantedBy=multi-user.target
'''
    Path("/etc/systemd/system/memoryscope.service").write_text(service)
    if args.enable_probe:
        if (
            not (botroot / "main.py").is_file()
            or not Path(args.astrbot_python).is_file()
        ):
            parser.error("AstrBot entrypoint/Python not found")
        dropin = (
            Path("/etc/systemd/system") / (args.unit + ".d") / "90-memoryscope.conf"
        )
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(f'''# Managed by MemoryScope; remove only this drop-in to restore the original ExecStart.
[Unit]
Wants=memoryscope.service
After=memoryscope.service
[Service]
ExecStart=
ExecStart="{args.astrbot_python}" "{destination}/entry.py" launch --config "{config_file}" "{botroot}/main.py"
''')
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "memoryscope.service"], check=True)
    subprocess.run(["systemctl", "restart", "memoryscope.service"], check=True)
    print(
        json.dumps(
            {
                "installed": str(destination),
                "backup": str(backup),
                "probe_on_next_start": args.enable_probe,
                "astrbot_restarted": False,
            }
        )
    )


if __name__ == "__main__":
    main()
