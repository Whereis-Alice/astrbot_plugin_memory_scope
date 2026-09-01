"""Sampling history, baselines, growth trends and threshold alerts."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

BYTES_PER_MB = 1024 * 1024
PAYLOAD_VERSION = 2
PROCESS_KEY = "__process__"


@dataclass(slots=True)
class Sample:
    """One tick of the background loop.

    ``rss_bytes`` is always present because reading it costs microseconds.  The
    per-plugin numbers come from the object census, which is opt-in, so most
    samples are RSS-only.
    """

    ts: float
    rss_bytes: int
    census_bytes: int = 0
    plugins: dict[str, int] = field(default_factory=dict)
    # A real census can legitimately find zero plugin-owned objects.  Keep an
    # explicit marker so that an empty result is not confused with an RSS-only
    # sample (and so it can still be used as a valid baseline).
    census_ran: bool = False

    def __post_init__(self) -> None:
        # Preserve the intuitive behaviour for callers constructing a Sample
        # directly: non-empty census fields imply that a census was performed.
        # The collector passes ``census_ran=True`` for the valid empty result.
        if not self.census_ran and (self.census_bytes > 0 or self.plugins):
            self.census_ran = True

    @property
    def has_attribution(self) -> bool:
        """Whether an object census ran for this sample.

        RSS-only samples must never be mixed into per-plugin series: a missing
        plugin would read as "dropped to 0" and invent a fake leak recovery.
        """

        return self.census_ran or self.census_bytes > 0 or bool(self.plugins)

    def to_payload(self) -> list[Any]:
        return [
            round(self.ts, 3),
            self.rss_bytes,
            self.census_bytes,
            self.plugins,
            self.census_ran,
        ]

    @classmethod
    def from_payload(cls, payload: Any, attribution: bool = True) -> "Sample | None":
        if not isinstance(payload, (list, tuple)) or len(payload) < 4:
            return None
        try:
            plugins = payload[3] if isinstance(payload[3], dict) else {}
            if not attribution:
                # Older payloads carry tracemalloc numbers in the same slots.
                # They are not comparable with census bytes, so keep only RSS.
                return cls(ts=float(payload[0]), rss_bytes=int(payload[1]))
            census_ran = (
                bool(payload[4])
                if len(payload) >= 5
                else int(payload[2]) > 0 or bool(plugins)
            )
            return cls(
                ts=float(payload[0]),
                rss_bytes=int(payload[1]),
                census_bytes=int(payload[2]),
                plugins={str(k): int(v) for k, v in plugins.items()},
                census_ran=census_ran,
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

    def replace_samples(self, samples: Iterable[Sample]) -> None:
        """Replace the ring buffer after a live history-size change."""

        self._samples = deque(samples, maxlen=self.max_samples)

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

    def census_samples(self, limit: int | None = None) -> list[Sample]:
        """Samples that carry per-plugin numbers, newest last."""

        items = [sample for sample in self._samples if sample.has_attribution]
        if limit and limit > 0:
            items = items[-limit:]
        return items

    def series(self, plugin: str, limit: int | None = None) -> list[list[float]]:
        return [
            [round(sample.ts, 3), int(sample.plugins.get(plugin, 0))]
            for sample in self.census_samples(limit)
        ]

    def rss_series(self, limit: int | None = None) -> list[list[float]]:
        return [[round(sample.ts, 3), sample.rss_bytes] for sample in self.samples(limit)]

    def totals_series(self, limit: int | None = None) -> list[list[float]]:
        return [
            [round(sample.ts, 3), sample.rss_bytes, sample.census_bytes]
            for sample in self.samples(limit)
        ]

    def delta(self, plugin: str, current_bytes: int) -> int | None:
        if self._baseline is None or not self._baseline.has_attribution:
            # A baseline pinned without a census carries no per-plugin numbers,
            # so any difference against it would be pure noise.
            return None
        return current_bytes - int(self._baseline.plugins.get(plugin, 0))

    def rss_delta(self, current_bytes: int) -> int | None:
        if self._baseline is None:
            return None
        return current_bytes - int(self._baseline.rss_bytes)

    def trend_bytes_per_minute(self, plugin: str, window: int = 20) -> float | None:
        """Least-squares slope of the plugin series, in bytes per minute."""

        return _slope(
            [
                (sample.ts, float(sample.plugins.get(plugin, 0)))
                for sample in self.census_samples(window)
            ],
        )

    def rss_trend_bytes_per_minute(self, window: int = 20) -> float | None:
        """Process RSS slope; the one trend that works with everything off."""

        return _slope(
            [(sample.ts, float(sample.rss_bytes)) for sample in self.samples(window)],
        )

    # ------------------------------------------------------------------
    def to_payload(self, keep: int = 240) -> dict[str, Any]:
        return {
            "version": PAYLOAD_VERSION,
            "samples": [s.to_payload() for s in self.samples(keep)],
            "baseline": self._baseline.to_payload() if self._baseline else None,
        }

    def load_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        try:
            version = int(payload.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        attribution = version >= PAYLOAD_VERSION
        for raw in payload.get("samples") or []:
            sample = Sample.from_payload(raw, attribution=attribution)
            if sample is not None:
                self._samples.append(sample)
        baseline = Sample.from_payload(payload.get("baseline"), attribution=attribution)
        if baseline is not None:
            self._baseline = baseline


def _slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope in units per minute, or None when undecidable."""

    if len(points) < 3:
        return None
    if points[-1][0] - points[0][0] <= 0:
        return None
    mean_t = sum(p[0] for p in points) / len(points)
    mean_v = sum(p[1] for p in points) / len(points)
    numerator = sum((p[0] - mean_t) * (p[1] - mean_v) for p in points)
    denominator = sum((p[0] - mean_t) ** 2 for p in points)
    if denominator <= 0:
        return None
    return (numerator / denominator) * 60.0


@dataclass(slots=True)
class Alert:
    ts: float
    plugin: str
    kind: str
    message: str
    value: float


class AlertEngine:
    """Threshold and growth alerts with a per-target cooldown."""

    def __init__(
        self,
        size_mb: float = 0.0,
        growth_mb_per_hour: float = 0.0,
        rss_mb: float = 0.0,
        rss_growth_mb_per_hour: float = 0.0,
        cooldown_seconds: float = 1800.0,
        max_alerts: int = 50,
    ) -> None:
        self.size_mb = float(size_mb or 0.0)
        self.growth_mb_per_hour = float(growth_mb_per_hour or 0.0)
        self.rss_mb = float(rss_mb or 0.0)
        self.rss_growth_mb_per_hour = float(rss_growth_mb_per_hour or 0.0)
        self.cooldown_seconds = max(60.0, float(cooldown_seconds))
        self._last_fired: dict[tuple[str, str], float] = {}
        self._alerts: deque[Alert] = deque(maxlen=max(1, int(max_alerts)))

    def update_thresholds(
        self,
        size_mb: float,
        growth_mb_per_hour: float,
        rss_mb: float = 0.0,
        rss_growth_mb_per_hour: float = 0.0,
    ) -> None:
        self.size_mb = float(size_mb or 0.0)
        self.growth_mb_per_hour = float(growth_mb_per_hour or 0.0)
        self.rss_mb = float(rss_mb or 0.0)
        self.rss_growth_mb_per_hour = float(rss_growth_mb_per_hour or 0.0)

    @property
    def plugin_rules_enabled(self) -> bool:
        return self.size_mb > 0 or self.growth_mb_per_hour > 0

    @property
    def process_rules_enabled(self) -> bool:
        return self.rss_mb > 0 or self.rss_growth_mb_per_hour > 0

    @property
    def enabled(self) -> bool:
        return self.plugin_rules_enabled or self.process_rules_enabled

    def alerts(self, limit: int = 20) -> list[Alert]:
        items = list(self._alerts)
        if limit <= 0:
            return items
        return items[-limit:]

    def evaluate(
        self,
        rows: Iterable[dict[str, Any]],
        now: float | None = None,
    ) -> list[Alert]:
        """Per-plugin rules, driven by the object census."""

        if not self.plugin_rules_enabled:
            return []
        now = time.time() if now is None else now
        fired: list[Alert] = []
        for row in rows:
            plugin = str(row.get("name") or "")
            if not plugin:
                continue
            size_bytes = int(row.get("census_bytes") or 0)
            trend = row.get("trend_bytes_per_minute")
            if self.size_mb > 0 and size_bytes >= self.size_mb * BYTES_PER_MB:
                alert = self._fire(
                    now,
                    plugin,
                    "size",
                    f"{plugin} 普查对象合计 {size_bytes / BYTES_PER_MB:.1f} MB，"
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

    def evaluate_process(
        self,
        rss_bytes: int,
        trend_bytes_per_minute: float | None = None,
        now: float | None = None,
    ) -> list[Alert]:
        """Process-level rules.  These work with every probe switched off."""

        if not self.process_rules_enabled:
            return []
        now = time.time() if now is None else now
        fired: list[Alert] = []
        rss_mb = float(rss_bytes) / BYTES_PER_MB
        if self.rss_mb > 0 and rss_mb >= self.rss_mb:
            alert = self._fire(
                now,
                PROCESS_KEY,
                "rss",
                f"AstrBot 进程 RSS {rss_mb:.0f} MB，超过阈值 {self.rss_mb:.0f} MB",
                rss_mb,
            )
            if alert:
                fired.append(alert)
        if self.rss_growth_mb_per_hour > 0 and isinstance(
            trend_bytes_per_minute, (int, float),
        ):
            per_hour = float(trend_bytes_per_minute) * 60.0 / BYTES_PER_MB
            if per_hour >= self.rss_growth_mb_per_hour:
                alert = self._fire(
                    now,
                    PROCESS_KEY,
                    "rss_growth",
                    f"AstrBot 进程 RSS 持续增长 {per_hour:.2f} MB/小时，"
                    f"超过阈值 {self.rss_growth_mb_per_hour:.2f} MB/小时",
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
        # The first alert for a (target, kind) pair always fires; the cooldown
        # only throttles repeats.
        if last is not None and now - last < self.cooldown_seconds:
            return None
        self._last_fired[key] = now
        alert = Alert(ts=now, plugin=plugin, kind=kind, message=message, value=value)
        self._alerts.append(alert)
        return alert
