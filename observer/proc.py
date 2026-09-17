"""Read kernel counters, retaining missing values and sampling timestamps."""

from __future__ import annotations

import os
import time
from pathlib import Path


def text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return ""


def integer(path: Path) -> int | None:
    try:
        return int(text(path).strip())
    except ValueError:
        return None


def fields(path: Path, *, kb: bool = False) -> dict[str, int]:
    result = {}
    for line in text(path).splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            try:
                result[parts[0]] = int(parts[1]) * (1024 if kb else 1)
            except ValueError:
                pass
    return result


def identity(pid: int, proc: Path = Path("/proc")) -> tuple[int, int] | None:
    raw = text(proc / str(pid) / "stat")
    try:
        tail = raw[raw.rindex(")") + 2 :].split()
        return int(tail[1]), int(tail[19])  # ppid, kernel start ticks
    except (ValueError, IndexError):
        return None


def boot_id(proc: Path = Path("/proc")) -> str:
    return text(proc / "sys/kernel/random/boot_id").strip() or "unknown-boot"


def process(
    pid: int, *, detailed: bool = False, proc: Path = Path("/proc")
) -> dict | None:
    ident = identity(pid, proc)
    if ident is None:
        return None
    status = fields(proc / str(pid) / "status", kb=True)
    result = {
        "pid": pid,
        "ppid": ident[0],
        "start_ticks": ident[1],
        "name": text(proc / str(pid) / "comm").strip(),
        "rss": status.get("VmRSS"),
        "swap": status.get("VmSwap"),
        "sampled_at": time.time(),
    }
    # Deliberately do not collect argv or environment: both may contain secrets.
    try:
        result["executable"] = os.readlink(proc / str(pid) / "exe")
    except OSError:
        result["executable"] = None
    if detailed:
        rollup = fields(proc / str(pid) / "smaps_rollup", kb=True)
        result.update(
            pss=rollup.get("Pss"),
            swap_pss=rollup.get("SwapPss"),
            private_dirty=rollup.get("Private_Dirty"),
            detailed_at=time.time(),
        )
    if identity(pid, proc) != ident:
        return None
    return result


def cgroup_pids(group: Path) -> list[int]:
    # Descendant cgroups are part of the service accounting too.
    paths = [group / "cgroup.procs"]
    try:
        paths.extend(group.glob("**/cgroup.procs"))
    except OSError:
        pass
    result: set[int] = set()
    for path in paths:
        result.update(int(x) for x in text(path).split() if x.isdigit())
    return sorted(result)


def cgroup_memory(group: Path) -> dict:
    stat = fields(group / "memory.stat")
    anon, swap = stat.get("anon"), integer(group / "memory.swap.current")
    return {
        "current": integer(group / "memory.current"),
        "anon": anon,
        "file": stat.get("file"),
        "swap": swap,
        "anon_swap": anon + swap if anon is not None and swap is not None else None,
        "limit": integer(group / "memory.max"),
        "events": fields(group / "memory.events"),
        "pressure": text(group / "memory.pressure").strip(),
    }


def host_memory(proc: Path = Path("/proc")) -> dict:
    mem = fields(proc / "meminfo", kb=True)
    vm = fields(proc / "vmstat")
    return {
        "total": mem.get("MemTotal"),
        "available": mem.get("MemAvailable"),
        "swap_total": mem.get("SwapTotal"),
        "swap_free": mem.get("SwapFree"),
        "swap_in_pages": vm.get("pswpin"),
        "swap_out_pages": vm.get("pswpout"),
        "pressure": text(proc / "pressure/memory").strip(),
    }
