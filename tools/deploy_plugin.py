#!/usr/bin/env python3
"""Copy plugin code after backing it up, preserving all runtime data and config."""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--astrbot-root", default="/root/AstrBot")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    botroot = Path(args.astrbot_root).resolve()
    destination = botroot / "data/plugins/astrbot_plugin_memory_scope"
    backup = Path("/root/memoryscope-backups") / (
        "plugin-"
        + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    )
    if destination.exists():
        backup.mkdir(parents=True, mode=0o700)
        shutil.copytree(destination, backup / destination.name)
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("core", "pages", ".astrbot-plugin", "docs"):
        shutil.copytree(
            source / name,
            destination / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in (
        "main.py",
        "metadata.yaml",
        "_conf_schema.json",
        "requirements.txt",
        "README.md",
        "changelog.md",
        "LICENSE",
    ):
        shutil.copy2(source / name, destination / name)
    print(
        json.dumps(
            {
                "plugin": str(destination),
                "backup": str(backup) if backup.exists() else None,
                "astrbot_restarted": False,
            }
        )
    )


if __name__ == "__main__":
    main()
