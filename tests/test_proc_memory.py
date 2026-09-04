"""Kernel footprint reader: parsing, TTL caching and graceful degradation."""

from __future__ import annotations

from core import proc_memory
from core.proc_memory import (
    SmapsRollupReader,
    parse_rollup,
    read_rollup,
)

KB = 1024

#: Verbatim /proc/598/smaps_rollup from the 2-core / 1676 MB Debian box this
#: plugin was tuned against.  RSS said 601 MB; 366 MB more were sitting in swap.
REAL_ROLLUP = """55a1c8a00000-7ffd0b5ff000 ---p 00000000 00:00 0                          [rollup]
Rss:              601268 kB
Pss:              598632 kB
Pss_Dirty:        476464 kB
Pss_Anon:         470000 kB
Shared_Clean:       4228 kB
Shared_Dirty:          0 kB
Private_Clean:    120576 kB
Private_Dirty:    476464 kB
Referenced:       590000 kB
Anonymous:        470000 kB
AnonHugePages:     38912 kB
Swap:             375160 kB
SwapPss:          375160 kB
Locked:                0 kB
"""


def test_parse_rollup_reads_the_real_thing_in_bytes():
    values = parse_rollup(REAL_ROLLUP)

    assert values["rollup_rss_bytes"] == 601268 * KB
    assert values["pss_bytes"] == 598632 * KB
    assert values["swap_pss_bytes"] == 375160 * KB
    assert values["private_dirty_bytes"] == 476464 * KB
    assert values["shared_clean_bytes"] == 4228 * KB
    assert values["anon_huge_pages_bytes"] == 38912 * KB


def test_footprint_and_private_totals_are_derived_not_reported():
    values = parse_rollup(REAL_ROLLUP)

    # The headline number: Pss + SwapPss = 973 MB against an RSS of 601 MB.
    assert values["footprint_bytes"] == (598632 + 375160) * KB
    assert values["footprint_bytes"] > values["rollup_rss_bytes"]
    assert values["private_bytes"] == (120576 + 476464) * KB


def test_parse_rollup_accepts_bytes_and_tolerates_junk():
    values = parse_rollup(REAL_ROLLUP.encode("utf-8"))
    assert values["pss_bytes"] == 598632 * KB

    tolerant = parse_rollup("Pss: 10 kB\nRss: not-a-number kB\nnocolon\nSwapPss:\n")
    assert tolerant["pss_bytes"] == 10 * KB
    assert "rollup_rss_bytes" not in tolerant
    # SwapPss with no value must not fabricate a swap figure.
    assert tolerant["footprint_bytes"] == 10 * KB


def test_a_rollup_without_pss_counts_as_unsupported():
    # Kernels built without CONFIG_PROC_PAGE_MONITOR expose the file but omit
    # Pss.  Everything left is already in /proc/self/status, so report nothing
    # and let callers fall back on RSS with one check instead of two.
    assert parse_rollup("Rss: 100 kB\nSwap: 20 kB\n") == {}
    assert parse_rollup("") == {}
    assert parse_rollup(b"\xff\xfe") == {}


def test_read_rollup_returns_empty_for_a_missing_path(tmp_path):
    assert read_rollup(str(tmp_path / "nope")) == {}

    path = tmp_path / "rollup"
    path.write_text(REAL_ROLLUP, encoding="utf-8")
    assert read_rollup(str(path))["pss_bytes"] == 598632 * KB


def test_reader_serves_the_cache_until_the_ttl_expires(tmp_path):
    path = tmp_path / "rollup"
    path.write_text(REAL_ROLLUP, encoding="utf-8")
    reader = SmapsRollupReader(min_interval_seconds=30.0, path=str(path))

    first = reader.read()
    assert first["pss_bytes"] == 598632 * KB
    assert reader.stats()["reads"] == 1
    assert reader.fresh is True

    # 5.4 ms per read is far too slow for the event loop, so a second caller
    # inside the window gets the cached copy plus a staleness marker.
    path.write_text(REAL_ROLLUP.replace("598632", "111111"), encoding="utf-8")
    cached = reader.read()
    assert cached["pss_bytes"] == 598632 * KB
    assert reader.stats()["reads"] == 1
    assert cached["rollup_age_seconds"] >= 0.0
    assert "rollup_generated_at" in cached

    # force is what the sampler thread uses to refresh it.
    forced = reader.read(force=True)
    assert forced["pss_bytes"] == 111111 * KB
    assert reader.stats()["reads"] == 2


def test_reader_keeps_the_last_good_values_when_pss_disappears(tmp_path):
    path = tmp_path / "rollup"
    path.write_text(REAL_ROLLUP, encoding="utf-8")
    reader = SmapsRollupReader(min_interval_seconds=0.0, path=str(path))
    assert reader.read()["pss_bytes"] == 598632 * KB

    path.write_text("Rss: 100 kB\n", encoding="utf-8")
    stale = reader.read(force=True)
    assert stale["pss_bytes"] == 598632 * KB
    assert reader.stats()["failures"] == 1


def test_reader_stops_paying_for_syscalls_once_the_file_is_gone(tmp_path):
    path = tmp_path / "rollup"
    path.write_text(REAL_ROLLUP, encoding="utf-8")
    reader = SmapsRollupReader(min_interval_seconds=0.0, path=str(path))
    assert reader.read() != {}

    path.unlink()
    assert reader.read(force=True) == {}
    assert reader.supported is False
    # supported latched to False, so no further open() attempts.
    before = reader.stats()["failures"]
    assert reader.read(force=True) == {}
    assert reader.stats()["failures"] == before


def test_reader_reports_unsupported_without_touching_the_disk(tmp_path):
    reader = SmapsRollupReader(path=str(tmp_path / "missing"))

    assert reader.supported is False
    assert reader.read() == {}
    assert reader.fresh is False
    stats = reader.stats()
    assert stats == {
        "supported": False,
        "reads": 0,
        "failures": 0,
        "elapsed_ms": 0.0,
        "min_interval_seconds": proc_memory.DEFAULT_MIN_INTERVAL_SECONDS,
        "generated_at": None,
    }


def test_negative_intervals_are_clamped_and_meta_keys_are_declared():
    reader = SmapsRollupReader(min_interval_seconds=-5.0, path="/nonexistent")
    assert reader.min_interval_seconds == 0.0

    # Consumers that only want the raw kernel numbers filter these out.
    assert set(proc_memory.META_KEYS) == {
        "rollup_age_seconds",
        "rollup_elapsed_ms",
        "rollup_generated_at",
    }
