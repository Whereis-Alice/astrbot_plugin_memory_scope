"""Independent cgroup sampler and authenticated, bounded probe event receiver."""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import __version__, proc
from .activity import clean_record, growth_report, number
from .analysis import startup_report, summarize_run
from .config import Config
from .store import Store


class Observer:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(
            Path(config.state_dir) / "history.sqlite3",
            max_samples=config.max_samples,
            retention_days=config.retention_days,
            max_mb=config.max_database_mb,
        )
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.group = Path(config.cgroup) if config.cgroup else None
        self.own_group = None
        for line in proc.text(Path("/proc/self/cgroup")).splitlines():
            if line.startswith("0::"):
                self.own_group = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
        self.boot = proc.boot_id()
        self.current_run: dict | None = None
        self.latest: dict | None = None
        self.host, self.processes = {}, []
        self._detailed = {}
        self._resolved = self._process_at = self._smaps_at = self._host_at = 0.0
        self.started = time.time()
        self.cpu_started = time.process_time()
        self.sampling_ms = self.max_sampling_ms = 0.0
        self.sampling_count = self.event_rejected = 0
        self.error: str | None = None
        self.threads = []
        self.events_socket: socket.socket | None = None
        self.journal = None

    def resolve(self):
        now = time.monotonic()
        if self.config.cgroup or now - self._resolved < 5:
            return
        self._resolved = now
        result = subprocess.run(
            [
                "systemctl",
                "show",
                self.config.unit,
                "--property=ControlGroup",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        value = result.stdout.strip()
        if not value.startswith("/") or value == "/":
            self.group = None
            return
        root = Path("/sys/fs/cgroup").resolve()
        group = (root / value.lstrip("/")).resolve()
        if group.is_relative_to(root) and group != root:
            self.group = group

    def _run_for(self, pid: int, ticks: int, now: float) -> dict:
        run_id = f"{self.boot}:{pid}:{ticks}"
        if self.current_run and self.current_run["id"] == run_id:
            return self.current_run
        if self.current_run and not self.current_run.get("ended_at"):
            self.current_run["ended_at"] = now
            self.store.save_run(self.current_run)
        run = self.store.run(run_id) or {
            "id": run_id,
            "pid": pid,
            "start_ticks": ticks,
            "started_at": now,
            "ready_at": None,
            "inventory": [],
            "source": "cgroup_v2",
            "unit": self.config.unit,
        }
        self.current_run = run
        self.processes = []
        self._detailed.clear()
        self._process_at = self._smaps_at = 0
        self.store.save_run(run)
        return run

    def _primary(self, pids: list[int]) -> tuple[int, int] | None:
        identities = {p: proc.identity(p) for p in pids}
        candidates = [
            (p, ident[1])
            for p, ident in identities.items()
            if ident and ident[0] not in identities
        ]
        return min(candidates, key=lambda item: item[1]) if candidates else None

    def sample(self):
        started = time.perf_counter()
        self.resolve()
        now, mono = time.time(), time.monotonic()
        group = self.group
        pids = proc.cgroup_pids(group) if group else []
        main = self._primary(pids)
        if mono - self._host_at >= self.config.interval:
            self.host = proc.host_memory()
            self._host_at = mono
        if not main:
            with self.lock:
                if self.current_run and not self.current_run.get("ended_at"):
                    self.current_run["ended_at"] = now
                    self.store.save_run(self.current_run)
                self.latest = {
                    "ts": now,
                    "state": "stopped",
                    "host": self.host,
                    "processes": [],
                }
            return
        with self.lock:
            run = self._run_for(main[0], main[1], now)
            is_startup = (
                not run.get("ready_at")
                and now - run["started_at"] < self.config.startup_seconds
            )
        if mono - self._process_at >= self.config.process_interval:
            detailed = mono - self._smaps_at >= self.config.smaps_interval
            processes = []
            for pid in pids:
                row = proc.process(pid, detailed=detailed)
                if row is None:
                    continue
                key = (pid, row["start_ticks"])
                if detailed:
                    self._detailed[key] = {
                        k: row.get(k)
                        for k in ("pss", "swap_pss", "private_dirty", "detailed_at")
                    }
                else:
                    row.update(self._detailed.get(key, {}))
                processes.append(row)
            previous = {(p["pid"], p["start_ticks"]): p for p in self.processes}
            current = {(p["pid"], p["start_ticks"]): p for p in processes}
            changes = []
            for key in previous.keys() ^ current.keys():
                row = current.get(key) or previous[key]
                changes.append(
                    {
                        "id": f"proc_{key[0]}_{key[1]}_{int(now * 1000)}",
                        "start": now,
                        "end": now,
                        "category": "process",
                        "plugin": "AstrBot",
                        "operation": f"{'appeared' if key in current else 'disappeared'}:{row.get('name', '')}:{key[0]}",
                        "status": "ok",
                        "duration_ms": 0,
                    }
                )
            if changes:
                self.store.save_activities(
                    run["id"], [clean_record(r, now) for r in changes[:256]]
                )
            self.processes = processes
            live = {(r["pid"], r["start_ticks"]) for r in processes}
            self._detailed = {k: v for k, v in self._detailed.items() if k in live}
            self._process_at = mono
            if detailed:
                self._smaps_at = mono
            if not run.get("ready_at") and self.config.ready_port:
                try:
                    with socket.create_connection(
                        (self.config.ready_host, self.config.ready_port), timeout=0.1
                    ):
                        run["ready_at"] = now
                        run["readiness_source"] = "tcp_listener"
                        self.store.save_run(run)
                except OSError:
                    pass
        sample = {
            "ts": now,
            "monotonic": mono,
            "run_id": run["id"],
            "state": "starting" if is_startup else "running",
            "memory": proc.cgroup_memory(group),
            "host": self.host,
            "processes": self.processes,
        }
        with self.lock:
            self.latest = sample
        self.store.append("samples", run["id"], sample)
        elapsed = (time.perf_counter() - started) * 1000
        self.sampling_ms += elapsed
        self.max_sampling_ms = max(self.max_sampling_ms, elapsed)
        self.sampling_count += 1
        self.error = None

    def accept_event(self, payload: dict) -> bool:
        if not isinstance(payload, dict) or not hmac.compare_digest(
            str(payload.get("token", "")), self.config.token
        ):
            self.event_rejected += 1
            return False
        event = payload.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            return False
        try:
            pid, ticks = int(event["pid"]), int(event["start_ticks"])
            ts = float(event["ts"])
        except (KeyError, ValueError, TypeError):
            return False
        if (
            number(ts) is None
            or abs(time.time() - ts) > 60
            or proc.identity(pid) is None
        ):
            return False
        ident = proc.identity(pid)
        if ident is None or ident[1] != ticks or not self.group:
            return False
        if pid not in proc.cgroup_pids(self.group):
            return False
        with self.lock:
            main = self._primary(proc.cgroup_pids(self.group))
            if main is None or main[0] != pid:
                return False
            run = self._run_for(pid, ticks, ts)
            if event["kind"] == "activity_batch":
                records = event.get("records")
                if not isinstance(records, list) or len(records) > 100:
                    return False
                try:
                    records = [clean_record(item, time.time()) for item in records]
                except ValueError:
                    return False
                state = event.get("recorder") or {}
                if not isinstance(state, dict):
                    return False
                run["activity_status"] = {
                    "received_at": ts,
                    "enabled": state.get("enabled") is True,
                    "dropped": max(0, int(number(state.get("dropped")) or 0)),
                    "started_at": number(state.get("started_at")),
                    "handlers": max(0, int(number(state.get("handlers")) or 0)),
                    "overhead_ms": max(0, number(state.get("overhead_ms")) or 0),
                }
                self.store.save_activities(run["id"], records)
                self.store.save_run(run)
                return True
            if event["kind"] == "launch":
                run.update(
                    fingerprint=event.get("fingerprint"),
                    plan=event.get("plan"),
                    probe_enabled=event.get("probe_enabled"),
                    started_at=min(run["started_at"], ts),
                    launcher_ms=event.get("launcher_ms"),
                )
            elif event["kind"] == "inventory":
                inventory = event.get("plugins", [])
                if not isinstance(inventory, list) or len(inventory) > 2000:
                    return False
                run["inventory"] = inventory
                run["inventory_at"] = ts
                if event.get("astrbot_loaded"):
                    run.setdefault("astrbot_loaded_at", ts)
            elif event["kind"] == "probe_finished":
                run["probe_finished_at"] = ts
                run["probe_overhead_ms"] = event.get("overhead_ms")
                run["plan_applied"] = event.get("plan_applied", False)
            elif event["kind"] == "child_spawn":
                children = run.setdefault("children", [])
                if len(children) < 256:
                    children.append(
                        {
                            k: event.get(k)
                            for k in (
                                "plugin",
                                "child_pid",
                                "child_start_ticks",
                                "attribution",
                                "ts",
                            )
                        }
                    )
            elif event["kind"] == "diagnostic":
                snapshot = event.get("snapshot")
                if not isinstance(snapshot, dict) or len(snapshot) > 2000:
                    return False
                run["diagnostic_at"] = ts
                run["diagnostic_source"] = snapshot.get("source", "unknown")
                self.store.save_diagnostic(run["id"], snapshot, ts)
            if event["kind"] in {
                "launch",
                "inventory",
                "probe_finished",
                "child_spawn",
                "diagnostic",
            }:
                self.store.save_run(run)
            event_record = dict(event)
            if event_record.get("kind") == "diagnostic":
                event_record.pop("snapshot", None)
                event_record["plugin_count"] = len(
                    (event.get("snapshot") or {}).get("plugins", [])
                )
            self.store.append("events", run["id"], event_record)
        return True

    def activities(self, run_id, *, include_memory=True, **params):
        run = self.store.run(run_id)
        if not run:
            raise ValueError("Unknown run")
        result = self.store.activities(run_id, **params)
        if include_memory:
            self.store.activity_memory(
                run_id,
                result["items"],
                max_distance=max(10, min(30, self.config.interval * 2)),
            )
        return {
            "run_id": run_id,
            "recorder": run.get("activity_status"),
            "plugin_options": self.store.activity_plugins(run_id),
            **result,
        }

    def growth(self, run_id, since, until):
        run = self.store.run(run_id)
        if not run:
            raise ValueError("Unknown run")
        if (
            number(since) is None
            or number(until) is None
            or not 0 < until - since <= 86400
        ):
            raise ValueError("Select a finite interval of at most 24 hours")
        samples, summary = self.store.growth_samples(run_id, since, until)
        report = growth_report(samples, run, since, until)
        if report["available"]:
            report.update(summary)
        events = self.activities(
            run_id, since=since, until=until, limit=200, include_memory=False
        )
        report.update(
            run_id=run_id,
            activities=events["items"],
            events_truncated=events["next_cursor"] is not None,
            recorder=events["recorder"],
        )
        return report

    def overview(self) -> dict:
        with self.lock:
            latest = dict(self.latest or {})
            run = dict(self.current_run or {})
        owners = {
            (c["child_pid"], c["child_start_ticks"]): c
            for c in run.get("children", [])
            if c.get("child_start_ticks")
        }
        latest["processes"] = [
            dict(
                p,
                startup_owner=(owners.get((p["pid"], p["start_ticks"])) or {}).get(
                    "plugin"
                ),
            )
            for p in latest.get("processes", [])
        ]
        self_mem = proc.process(os.getpid()) or {}
        age = time.time() - latest["ts"] if "ts" in latest else None
        return {
            "version": __version__,
            "latest": latest,
            "run": run,
            "source": "independent_observer",
            "age_seconds": age,
            "stale": age is None or age > max(15, self.config.interval * 3),
            "config": self.config.public(),
            "error": self.error,
            "observer": {
                "pid": os.getpid(),
                "rss": self_mem.get("rss"),
                "cpu_seconds": time.process_time() - self.cpu_started,
                "uptime_seconds": time.time() - self.started,
                "sampling_count": self.sampling_count,
                "sampling_ms": self.sampling_ms,
                "max_sampling_ms": self.max_sampling_ms,
                "history_dropped": self.store.dropped,
                "event_rejected": self.event_rejected,
                "service_memory": proc.cgroup_memory(self.own_group)
                if self.own_group and self.own_group != self.group
                else None,
            },
        }

    def report(self, run_id: str, *, include_samples=True) -> dict:
        run = self.store.run(run_id)
        if not run:
            raise ValueError("Unknown run")
        events = self.store.rows("events", run_id, 50000)
        until = (
            None
            if include_samples
            else max(
                (
                    e["ts"]
                    for e in events
                    if e["kind"] in {"phase_end", "log_plugin_start"}
                ),
                default=run["started_at"],
            )
            + 0.3
        )
        result = startup_report(
            run,
            self.store.rows("samples", run_id, 50000, until=until, compact=True),
            events,
        )
        if not include_samples:
            result.pop("samples", None)
        return result

    def diagnostics(self, run_id: str | None = None) -> dict:
        selected = run_id or (self.current_run or {}).get("id", "")
        saved = self.store.diagnostic(selected) if selected else None
        if saved:
            return {"run_id": selected, **saved}
        if not run_id:
            latest = self.store.latest_diagnostic()
            if latest:
                return latest
        return {"run_id": selected or None, "snapshot": None}

    def trend(self, run_id: str, seconds: int = 3600) -> dict:
        run = self.store.run(run_id)
        if not run:
            raise ValueError("Unknown run")
        end = run.get("ended_at") or time.time()
        points = self.store.rows(
            "samples",
            run_id,
            50000,
            since=end - seconds if seconds else None,
            compact=True,
        )
        # Keep first/last and metric extrema; never load historical process trees.
        count = len(points)
        if count > 1800:
            step = (count + 179) // 180
            selected = {}
            for index in range(0, count, step):
                bucket = points[index : index + step]
                for item in (bucket[0], bucket[-1]):
                    selected[item["ts"]] = item
                for key in ("current", "anon_swap", "swap"):
                    missing = [
                        p for p in bucket if p.get("memory", {}).get(key) is None
                    ]
                    if missing:
                        # A missing sample is a discontinuity, never a zero or
                        # a line interpolated across an unavailable metric.
                        selected[missing[0]["ts"]] = missing[0]
                    valid = [
                        p for p in bucket if p.get("memory", {}).get(key) is not None
                    ]
                    if valid:
                        for item in (
                            min(valid, key=lambda p, k=key: p["memory"][k]),
                            max(valid, key=lambda p, k=key: p["memory"][k]),
                        ):
                            selected[item["ts"]] = item
            points = [selected[ts] for ts in sorted(selected)]
        return {
            "run_id": run_id,
            "samples": points,
            "source_points": count,
            "downsampled": len(points) < count,
            "seconds": seconds,
        }

    def summary(self, run_id: str) -> dict:
        run = self.store.run(run_id)
        if not run:
            raise ValueError("Unknown run")
        return summarize_run(
            run,
            self.store.rows("samples", run_id, 50000),
            self.config.settle_seconds,
            self.config.observe_seconds,
        )

    def _sample_loop(self):
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                self.sample()
                self.store.prune()
            except Exception as exc:  # noqa: BLE001 - isolate diagnostics/control failures
                self.error = f"Sampler: {type(exc).__name__}"
            run = self.current_run or {}
            startup = (
                not run.get("ready_at")
                and time.time() - run.get("started_at", 0) < self.config.startup_seconds
            )
            # Keep fast sampling briefly after readiness to capture late startup tasks.
            startup = startup or (
                run.get("ready_at") and time.time() - run["ready_at"] < 10
            )
            interval = self.config.startup_interval if startup else self.config.interval
            self.stop.wait(max(0.01, interval - (time.monotonic() - t0)))

    def _event_loop(self):
        assert self.events_socket is not None
        while not self.stop.is_set():
            try:
                data, _ = self.events_socket.recvfrom(65536)
                self.accept_event(json.loads(data))
            except TimeoutError:
                continue
            except (OSError, ValueError, TypeError):
                self.event_rejected += 1

    def _journal_loop(self):
        """Fallback records only loader markers, never message bodies or secrets."""
        try:
            self.journal = subprocess.Popen(
                [
                    "journalctl",
                    "--unit",
                    self.config.unit,
                    "--grep=Loading plugin [A-Za-z0-9_-]+ ",
                    "--follow",
                    "--since=now",
                    "--output=json",
                    "--output-fields=MESSAGE,_PID,__REALTIME_TIMESTAMP",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in self.journal.stdout:
                if self.stop.is_set():
                    break
                if "Loading plugin " not in line:
                    continue
                try:
                    row = json.loads(line)
                    match = re.search(
                        r"Loading plugin ([A-Za-z0-9_-]+) ", row.get("MESSAGE", "")
                    )
                    if not match:
                        continue
                    pid = int(row["_PID"])
                    ident = proc.identity(pid)
                    if not ident:
                        continue
                    self.accept_event(
                        {
                            "token": self.config.token,
                            "event": {
                                "kind": "log_plugin_start",
                                "pid": pid,
                                "start_ticks": ident[1],
                                "ts": int(row["__REALTIME_TIMESTAMP"]) / 1e6,
                                "plugin": match[1],
                                "source": "journal_receipt_time",
                            },
                        }
                    )
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            pass

    def start(self):
        self.resolve()
        self.events_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.events_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.events_socket.bind(("127.0.0.1", self.config.event_port))
        self.events_socket.settimeout(0.5)
        for target in (self._sample_loop, self._event_loop, self._journal_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        if self.journal:
            self.journal.terminate()
            try:
                self.journal.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.journal.kill()
                self.journal.wait(timeout=2)
        for thread in self.threads:
            thread.join(timeout=5)
        if self.events_socket:
            self.events_socket.close()
        self.store.close()
