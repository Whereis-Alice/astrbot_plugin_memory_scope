"""Kernel-reported memory footprint for the current process.

RSS answers "how many pages are mapped right now", which is the wrong question
on a small VPS: pages that got swapped out disappear from RSS even though the
process still owns them.  On the box this plugin was tuned against, RSS read
601 MB while the real footprint was 973 MB -- 372 MB were hiding in swap.

/proc/self/smaps_rollup gives the kernel's own aggregation in a single read:

* Pss -- resident pages, with shared pages divided by the number of sharers;
* SwapPss -- the same accounting for pages the kernel pushed to swap;
* Private_Dirty -- pages only this process can be holding, i.e. the closest
  thing to "memory that would be freed if the process exited".

footprint_bytes = Pss + SwapPss is the number to show a user who wants to know
how much memory AstrBot is actually costing them.

Cost note: the rollup is aggregated by the kernel, but it still walks every VMA.
It measured 5.4 ms with 846 mappings, which is far too slow for the event loop
to touch on every request, so reads are cached behind a TTL and refreshed from
the sampler's worker thread.  Compare with /proc/self/status at 0.014 ms.
"""

from __future__ import annotations

import os
import time
from typing import Any

SMAPS_ROLLUP_PATH = "/proc/self/smaps_rollup"
DEFAULT_MIN_INTERVAL_SECONDS = 30.0

#: procfs field name -> payload key.  Every value in smaps_rollup is in kB.
FIELD_MAP: dict[str, str] = {
    "Rss": "rollup_rss_bytes",
    "Pss": "pss_bytes",
    "Pss_Dirty": "pss_dirty_bytes",
    "Shared_Clean": "shared_clean_bytes",
    "Shared_Dirty": "shared_dirty_bytes",
    "Private_Clean": "private_clean_bytes",
    "Private_Dirty": "private_dirty_bytes",
    "Anonymous": "anonymous_bytes",
    "AnonHugePages": "anon_huge_pages_bytes",
    "Swap": "rollup_swap_bytes",
    "SwapPss": "swap_pss_bytes",
}

#: Keys added by the reader rather than the kernel; consumers that want the raw
#: measurement only should skip these.
META_KEYS = ("rollup_age_seconds", "rollup_elapsed_ms", "rollup_generated_at")


def parse_rollup(raw: str | bytes) -> dict[str, int]:
    """Turn smaps_rollup text into a byte-valued mapping.

    Returns an empty dict when the text carries no Pss line: without Pss there
    is nothing here that RSS did not already tell us, so callers can treat an
    empty result as "unsupported" without a second check.
    """

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            return {}
    if not raw:
        return {}

    values: dict[str, int] = {}
    for line in raw.splitlines():
        head, sep, tail = line.partition(":")
        if not sep:
            continue
        key = FIELD_MAP.get(head.strip())
        if key is None:
            continue
        parts = tail.split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0]) * 1024
        except (TypeError, ValueError):
            continue

    if "pss_bytes" not in values:
        return {}

    values["footprint_bytes"] = values["pss_bytes"] + values.get("swap_pss_bytes", 0)
    values["private_bytes"] = values.get("private_clean_bytes", 0) + values.get(
        "private_dirty_bytes", 0,
    )
    return values


def read_rollup(path: str = SMAPS_ROLLUP_PATH) -> dict[str, int]:
    """One uncached read.  Returns {} on any failure."""

    try:
        with open(path, "rb") as handle:
            return parse_rollup(handle.read())
    except OSError:
        return {}


class SmapsRollupReader:
    """TTL-cached smaps_rollup reader.

    The TTL exists because the read is milliseconds, not microseconds.  The
    sampler primes the cache from its worker thread; anything running on the
    event loop gets the cached copy plus an age field so the UI can say how
    stale the number is instead of silently showing a lie.
    """

    def __init__(
        self,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        path: str = SMAPS_ROLLUP_PATH,
    ) -> None:
        self.path = path
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._values: dict[str, int] = {}
        self._stamp: float = 0.0
        self._wall: float = 0.0
        self._elapsed_ms: float = 0.0
        self._reads = 0
        self._failures = 0
        self._supported: bool | None = None

    # ------------------------------------------------------------------
    @property
    def supported(self) -> bool:
        if self._supported is None:
            try:
                self._supported = os.path.exists(self.path)
            except OSError:
                self._supported = False
        return bool(self._supported)

    @property
    def fresh(self) -> bool:
        """Whether the cached values came from within the current TTL window."""

        if not self._values or not self._stamp:
            return False
        return (time.monotonic() - self._stamp) <= self.min_interval_seconds

    # ------------------------------------------------------------------
    def read(self, force: bool = False) -> dict[str, int]:
        if not self.supported:
            return {}

        now = time.monotonic()
        if (
            not force
            and self._values
            and (now - self._stamp) < self.min_interval_seconds
        ):
            return self._decorate(now)

        started = time.monotonic()
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read()
        except OSError:
            # A vanished procfs entry is permanent for this process; stop
            # paying for the syscall on every sample.
            self._failures += 1
            self._supported = False
            return {}

        values = parse_rollup(raw)
        if not values:
            self._failures += 1
            # Kernels without CONFIG_PROC_PAGE_MONITOR expose the file but not
            # Pss.  Keep serving the last good numbers if we ever had any.
            return self._decorate(time.monotonic()) if self._values else {}

        self._values = values
        self._elapsed_ms = (time.monotonic() - started) * 1000.0
        self._stamp = time.monotonic()
        self._wall = time.time()
        self._reads += 1
        return self._decorate(self._stamp)

    def stats(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "reads": self._reads,
            "failures": self._failures,
            "elapsed_ms": round(self._elapsed_ms, 3),
            "min_interval_seconds": self.min_interval_seconds,
            "generated_at": self._wall or None,
        }

    # ------------------------------------------------------------------
    def _decorate(self, now: float) -> dict[str, int]:
        payload: dict[str, Any] = dict(self._values)
        payload["rollup_age_seconds"] = round(max(0.0, now - self._stamp), 1)
        payload["rollup_elapsed_ms"] = round(self._elapsed_ms, 3)
        payload["rollup_generated_at"] = self._wall or None
        return payload
