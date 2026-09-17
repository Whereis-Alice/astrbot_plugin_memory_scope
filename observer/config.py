"""Validated observer configuration. Secrets never enter reports."""

from __future__ import annotations

import dataclasses
import json
import math
import re
from pathlib import Path


@dataclasses.dataclass
class Config:
    state_dir: str = "/var/lib/memoryscope"
    unit: str = "astrbot.service"
    cgroup: str = ""  # Explicit cgroup v2 directory also supports Docker.
    host: str = "127.0.0.1"
    port: int = 8766
    event_port: int = 8767
    token: str = ""
    interval: float = 5.0
    startup_interval: float = 0.1
    startup_seconds: float = 180.0
    process_interval: float = 2.0
    smaps_interval: float = 30.0
    retention_days: int = 7
    max_samples: int = 50000
    max_database_mb: int = 128
    ready_host: str = "127.0.0.1"
    ready_port: int = 6185
    allow_experiments: bool = False
    settle_seconds: float = 60.0
    observe_seconds: float = 30.0
    ready_timeout: float = 240.0

    @classmethod
    def load(cls, path: str | Path) -> Config:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = {f.name for f in dataclasses.fields(cls)}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError("Unknown config keys: " + ", ".join(sorted(unknown)))
        result = cls(**raw)
        result.validate()
        return result

    def validate(self) -> None:
        for name in (
            "interval",
            "startup_interval",
            "startup_seconds",
            "process_interval",
            "smaps_interval",
            "settle_seconds",
            "observe_seconds",
            "ready_timeout",
        ):
            if not isinstance(getattr(self, name), (int, float)) or not math.isfinite(
                getattr(self, name)
            ):
                raise ValueError(f"{name} must be a finite number")
        if len(self.token) < 32:
            raise ValueError("token must contain at least 32 characters")
        if self.host not in ("127.0.0.1", "::1"):
            raise ValueError(
                "Bind to loopback; use an authenticated proxy or SSH tunnel for remote access"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", self.unit):
            raise ValueError("Invalid systemd unit")
        if not Path(self.state_dir).is_absolute():
            raise ValueError("state_dir must be absolute")
        if self.cgroup and not Path(self.cgroup).is_absolute():
            raise ValueError("cgroup must be absolute")
        if not (1 <= self.port <= 65535 and 1 <= self.event_port <= 65535):
            raise ValueError("Invalid port")
        if self.port == self.event_port:
            raise ValueError("HTTP and event ports must differ")
        if self.interval < 1 or self.startup_interval < 0.05:
            raise ValueError("Sampling intervals are too small")
        if not 10 <= self.startup_seconds <= 900:
            raise ValueError("startup_seconds must be 10..900")
        if self.process_interval < 1 or self.smaps_interval < 10:
            raise ValueError("Detailed process sampling is too frequent")
        if not 1 <= self.retention_days <= 90 or not 100 <= self.max_samples <= 200000:
            raise ValueError("Invalid retention budget")
        if not 16 <= self.max_database_mb <= 1024:
            raise ValueError("Invalid database size budget")
        if (
            self.settle_seconds < 5
            or self.observe_seconds < 5
            or self.ready_timeout < 10
        ):
            raise ValueError("Experiment windows are too short")
        if type(self.allow_experiments) is not bool:
            raise ValueError("allow_experiments must be a boolean")

    @property
    def plan_path(self) -> Path:
        return Path(self.state_dir) / "next-launch.json"

    def public(self) -> dict:
        return {
            key: value
            for key, value in dataclasses.asdict(self).items()
            if key not in {"token", "state_dir"}
        }
