"""Sampling history, baselines, growth trends and threshold alerts."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

BYTES_PER_MB = 1024 * 1024


async def wait_until_loaded(event: asyncio.Event, timeout: float) -> bool:
    """Wait for the "AstrBot finished loading" signal without waiting forever.

    ``on_astrbot_loaded`` is dispatched exactly once per process, from
    ``CoreLifecycle.start()``.  A plugin that is installed, enabled or reloaded
    from the Dashboard therefore never sees that hook, so an unbounded
    ``event.wait()`` would keep the sampler blocked for the rest of the process
    lifetime: no history, no trend, no alerts and no persistence.

    Returns ``True`` when the event was set, or when ``timeout`` is not positive
    (which means "wait as long as it takes").  Returns ``False`` when the
    deadline expired first; the timeout is reported rather than raised because
    the caller treats it as "assume a runtime install and start sampling".
    """

    if not timeout or timeout <= 0:
        await event.wait()
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        return False
    return True


@dataclass(slots=True)
class Sample:
    ts: float
    rss_bytes: int
    traced_bytes: int
    plugins: dict[str, int] = field(default_factory=dict)

    @property
    def has_attribution(self) -> bool:
        """Whether tracemalloc was running when this sample was taken.

        RSS-only samples are recorded while tracing is off so the process
        trend keeps working; they must never be mixed into per-plugin series,
        where a missing plugin would otherwise read as "dropped to 0".
        """

        return self.traced_bytes > 0 or bool(self.plugins)

    def to_payload(self) -> list[Any]:
        return [round(self.ts, 3), self.rss_bytes, self.traced_bytes, self.plugins]

    @classmethod
    def from_payload(cls, payload: Any) -> "Sample | None":
        if not isinstance(payload, (list, tuple)) or len(payload) < 4:
            return None
        try:
            plugins = payload[3] if isinstance(payload[3], dict) else {}
            return cls(
                ts=float(payload[0]),
                rss_bytes=int(payload[1]),
                traced_bytes=int(payload[2]),
                plugins={str(k): int(v) for k, v in plugins.items()},
            )
        except (TypeError, ValueError):
            return None


class HistoryStore:
    """Ring buffer of samples plus an optional user-pinned baseline."""

    def __init__(self, max_samples: int = 720) -> None:
        self.max_samples = max(10, int(max_samples))
        self._samples: deque[Sample] = deque(maxlen=self.max_samples)
        self._baseline: Sample | None = None

    # ------------------------------------------------------------------
    def add(self, sample: Sample) -> None:
        self._samples.append(sample)

    @property
    def latest(self) -> Sample | None:
        return self._samples[-1] if self._samples else None

    @property
    def baseline(self) -> Sample | None:
        return self._baseline

    def count(self) -> int:
        return len(self._samples)

    def set_baseline(self, sample: Sample | None = None) -> Sample | None:
        self._baseline = sample or self.latest
        return self._baseline

    def clear_baseline(self) -> None:
        self._baseline = None

    def samples(self, limit: int | None = None) -> list[Sample]:
        items = list(self._samples)
        if limit and limit > 0:
            items = items[-limit:]
        return items

    def traced_samples(self, limit: int | None = None) -> list[Sample]:
        """Samples taken while tracemalloc was on, newest last."""

        items = [sample for sample in self._samples if sample.has_attribution]
        if limit and limit > 0:
            items = items[-limit:]
        return items

    def series(self, plugin: str, limit: int | None = None) -> list[list[float]]:
        return [
            [round(sample.ts, 3), int(sample.plugins.get(plugin, 0))]
            for sample in self.traced_samples(limit)
        ]

    def totals_series(self, limit: int | None = None) -> list[list[float]]:
        return [
            [round(sample.ts, 3), sample.rss_bytes, sample.traced_bytes]
            for sample in self.samples(limit)
        ]

    def delta(self, plugin: str, current_bytes: int) -> int | None:
        if self._baseline is None or not self._baseline.has_attribution:
            # A baseline pinned while tracing was off carries no per-plugin
            # numbers, so any difference against it would be pure noise.
            return None
        return current_bytes - int(self._baseline.plugins.get(plugin, 0))

    def trend_bytes_per_minute(self, plugin: str, window: int = 20) -> float | None:
        """Least-squares slope of the plugin series, in bytes per minute."""

        points = [
            (sample.ts, float(sample.plugins.get(plugin, 0)))
            for sample in self.traced_samples(window)
        ]
        if len(points) < 3:
            return None
        span = points[-1][0] - points[0][0]
        if span <= 0:
            return None
        mean_t = sum(p[0] for p in points) / len(points)
        mean_v = sum(p[1] for p in points) / len(points)
        numerator = sum((p[0] - mean_t) * (p[1] - mean_v) for p in points)
        denominator = sum((p[0] - mean_t) ** 2 for p in points)
        if denominator <= 0:
            return None
        return (numerator / denominator) * 60.0

    # ------------------------------------------------------------------
    def to_payload(self, keep: int = 240) -> dict[str, Any]:
        return {
            "version": 1,
            "samples": [s.to_payload() for s in self.samples(keep)],
            "baseline": self._baseline.to_payload() if self._baseline else None,
        }

    def load_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        for raw in payload.get("samples") or []:
            sample = Sample.from_payload(raw)
            if sample is not None:
                self._samples.append(sample)
        baseline = Sample.from_payload(payload.get("baseline"))
        if baseline is not None:
            self._baseline = baseline


@dataclass(slots=True)
class Alert:
    ts: float
    plugin: str
    kind: str
    message: str
    value: float


class AlertEngine:
    """Threshold and growth alerts with a per-plugin cooldown."""

    def __init__(
        self,
        size_mb: float = 0.0,
        growth_mb_per_hour: float = 0.0,
        cooldown_seconds: float = 1800.0,
        max_alerts: int = 50,
    ) -> None:
        self.size_mb = float(size_mb or 0.0)
        self.growth_mb_per_hour = float(growth_mb_per_hour or 0.0)
        self.cooldown_seconds = max(60.0, float(cooldown_seconds))
        self._last_fired: dict[tuple[str, str], float] = {}
        self._alerts: deque[Alert] = deque(maxlen=max(5, max_alerts))

    def update_thresholds(self, size_mb: float, growth_mb_per_hour: float) -> None:
        self.size_mb = float(size_mb or 0.0)
        self.growth_mb_per_hour = float(growth_mb_per_hour or 0.0)

    @property
    def enabled(self) -> bool:
        return self.size_mb > 0 or self.growth_mb_per_hour > 0

    def alerts(self, limit: int = 20) -> list[Alert]:
        items = list(self._alerts)
        return items[-limit:] if limit > 0 else items

    def evaluate(
        self,
        rows: Iterable[dict[str, Any]],
        now: float | None = None,
    ) -> list[Alert]:
        if not self.enabled:
            return []
        now = now or time.time()
        fired: list[Alert] = []
        for row in rows:
            plugin = str(row.get("name") or "")
            if not plugin:
                continue
            size_bytes = int(row.get("attributed_bytes") or 0)
            trend = row.get("trend_bytes_per_minute")
            if self.size_mb > 0 and size_bytes >= self.size_mb * BYTES_PER_MB:
                alert = self._fire(
                    now,
                    plugin,
                    "size",
                    f"{plugin} 当前归因内存 {size_bytes / BYTES_PER_MB:.1f} MB，"
                    f"超过阈值 {self.size_mb:.1f} MB",
                    size_bytes / BYTES_PER_MB,
                )
                if alert:
                    fired.append(alert)
            if self.growth_mb_per_hour > 0 and isinstance(trend, (int, float)):
                per_hour = float(trend) * 60.0 / BYTES_PER_MB
                if per_hour >= self.growth_mb_per_hour:
                    alert = self._fire(
                        now,
                        plugin,
                        "growth",
                        f"{plugin} 持续增长 {per_hour:.2f} MB/小时，"
                        f"超过阈值 {self.growth_mb_per_hour:.2f} MB/小时",
                        per_hour,
                    )
                    if alert:
                        fired.append(alert)
        return fired

    def _fire(
        self,
        now: float,
        plugin: str,
        kind: str,
        message: str,
        value: float,
    ) -> Alert | None:
        key = (plugin, kind)
        last = self._last_fired.get(key)
        # The first alert for a (plugin, kind) pair always fires; the cooldown
        # only throttles repeats.
        if last is not None and now - last < self.cooldown_seconds:
            return None
        self._last_fired[key] = now
        alert = Alert(ts=now, plugin=plugin, kind=kind, message=message, value=value)
        self._alerts.append(alert)
        return alert