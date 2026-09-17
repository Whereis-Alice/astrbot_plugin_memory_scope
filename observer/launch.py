"""Run AstrBot in the same PID, with optional early instrumentation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import runpy
import sys
import time
from pathlib import Path

from .config import Config
from .probe import EventSink, StartupProbe


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(sys.version.encode())
    for path in [
        root / "main.py",
        root / "data/cmd_config.json",
        *sorted((root / "data/config").glob("*.json")),
        *sorted((root / "data/plugins").glob("*/metadata.yaml")),
    ]:
        try:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


def consume_plan(path: Path) -> dict:
    try:
        plan = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(plan, dict):
        return {}
    expires = plan.get("expires_at", 0)
    if (
        not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or plan.get("consumed")
        or expires < time.time()
    ):
        return {}
    if plan.get("mode") not in {"normal", "skip", "disable"}:
        return {}
    if plan["mode"] != "normal" and (
        not isinstance(plan.get("plugin"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", plan["plugin"])
    ):
        return {}
    # Atomic, single-launch plan. Repeated crash restarts return to normal.
    temp = path.with_suffix(".consumed.tmp")
    temp.write_text(json.dumps(dict(plan, consumed=True)), encoding="utf-8")
    os.replace(temp, path)
    return plan


def launch(config_path: str, script: str, args: list[str], *, probe_enabled=True):
    started = time.perf_counter_ns()
    absolute_config = str(Path(config_path).resolve())
    try:
        config = Config.load(absolute_config)
    except (OSError, ValueError, TypeError):
        # A monitoring config failure must not turn into a bot outage.
        target = Path(script).resolve()
        os.chdir(target.parent)
        sys.path.insert(0, str(target.parent))
        sys.argv = [str(target), *args]
        print(
            "MemoryScope config unavailable; starting AstrBot without observer integration.",
            file=sys.stderr,
        )
        runpy.run_path(str(target), run_name="__main__")
        return
    target = Path(script).resolve()
    if not target.is_file():
        raise ValueError("AstrBot entrypoint does not exist")
    os.chdir(target.parent)
    sys.path.insert(0, str(target.parent))
    sys.argv = [str(target), *args]
    try:
        plan = consume_plan(config.plan_path)
    except (OSError, ValueError, TypeError):
        plan = {}
    if plan.get("mode") in {"skip", "disable"} and not probe_enabled:
        raise ValueError("Experiment requires startup probe")
    try:
        sink = EventSink(config.event_port, config.token)
    except OSError:
        print(
            "MemoryScope event channel unavailable; starting AstrBot without probe.",
            file=sys.stderr,
        )
        runpy.run_path(str(target), run_name="__main__")
        return
    sink.emit(
        "launch",
        fingerprint=fingerprint(target.parent),
        probe_enabled=probe_enabled,
        plan={k: plan.get(k) for k in ("job_id", "mode", "plugin", "label")},
        launcher_ms=(time.perf_counter_ns() - started) / 1e6,
    )
    probe = StartupProbe(sink, plan) if probe_enabled else None
    if probe:
        try:
            probe.install()
        except Exception as exc:  # noqa: BLE001 - monitoring must not prevent startup
            sink.emit("probe_error", error=type(exc).__name__)
            probe.finish()
            probe = None
    else:
        sink.close()
    # Local plugin discovers the observer without persisting any secret in its config.
    os.environ["MEMORYSCOPE_OBSERVER_CONFIG"] = absolute_config
    os.environ["MEMORYSCOPE_EARLY_PROBE"] = "1" if probe else "0"
    try:
        runpy.run_path(str(target), run_name="__main__")
    finally:
        if probe:
            probe.finish()
