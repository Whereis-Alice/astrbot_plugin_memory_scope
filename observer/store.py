"""Bounded SQLite history, outside AstrBot's data directory."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path


def pack(value: object) -> bytes:
    return zlib.compress(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(), 3
    )


def unpack(value: bytes):
    return json.loads(zlib.decompress(value))


class Store:
    def __init__(self, path: Path, *, max_samples=50000, retention_days=7, max_mb=128):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.max_samples, self.retention_days = (
            path,
            max_samples,
            retention_days,
        )
        self.max_bytes = max_mb * 1024 * 1024
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=3)
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA wal_autocheckpoint=128")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, ts REAL, payload BLOB);
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, run TEXT, ts REAL, payload BLOB);
            CREATE INDEX IF NOT EXISTS sample_run ON samples(run, ts);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run TEXT, ts REAL, payload BLOB);
            CREATE INDEX IF NOT EXISTS event_run ON events(run, ts);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, ts REAL, payload BLOB);
            CREATE TABLE IF NOT EXISTS diagnostics(run TEXT PRIMARY KEY, ts REAL, payload BLOB);
            CREATE INDEX IF NOT EXISTS diagnostic_ts ON diagnostics(ts);
            CREATE TABLE IF NOT EXISTS activities(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL,
                uid TEXT NOT NULL, start REAL NOT NULL, end REAL,
                category TEXT NOT NULL, plugin TEXT NOT NULL, payload BLOB NOT NULL,
                UNIQUE(run,uid));
            CREATE INDEX IF NOT EXISTS activity_run ON activities(run,start);
        """)
        self.db.commit()
        self.dropped = 0
        self._pruned = 0.0

    def save_run(self, run: dict) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                (run["id"], run["started_at"], pack(run)),
            )

    def run(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return unpack(row[0]) if row else None

    def runs(self, limit=50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT payload FROM runs ORDER BY ts DESC LIMIT ?", (min(200, limit),)
            ).fetchall()
        return [unpack(row[0]) for row in rows]

    def append(self, table: str, run_id: str, value: dict) -> None:
        if table not in {"samples", "events"}:
            raise ValueError("Invalid history type")
        with self.lock, self.db:
            if self.path.stat().st_size > self.max_bytes:
                self.prune(force=True)
                if self.path.stat().st_size > self.max_bytes:
                    self.dropped += 1
                    return
            self.db.execute(
                f"INSERT INTO {table}(run,ts,payload) VALUES (?,?,?)",
                (run_id, value["ts"], pack(value)),
            )

    def rows(
        self,
        table: str,
        run_id: str,
        limit=10000,
        *,
        since=None,
        until=None,
        compact=False,
    ) -> list[dict]:
        if table not in {"samples", "events"}:
            raise ValueError("Invalid history type")
        where, params = ["run=?"], [run_id]
        if since is not None:
            where.append("ts>=?")
            params.append(since)
        if until is not None:
            where.append("ts<=?")
            params.append(until)
        params.append(min(limit, 50000))
        result = []
        with self.lock:
            cursor = self.db.execute(
                f"SELECT payload FROM {table} WHERE {' AND '.join(where)} ORDER BY ts DESC,id DESC LIMIT ?",
                params,
            )
            for row in cursor:
                value = unpack(row[0])
                if compact:
                    value = {
                        k: value[k] for k in ("ts", "memory", "state") if k in value
                    }
                result.append(value)
        result.reverse()
        return result

    def save_activities(self, run_id: str, records: list[dict]) -> None:
        """Separate quota: frequent runtime events never evict startup evidence."""
        with self.lock, self.db:
            if self.path.stat().st_size > self.max_bytes:
                self.dropped += len(records)
                return
            for item in records:
                self.db.execute(
                    "INSERT INTO activities(run,uid,start,end,category,plugin,payload) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(run,uid) DO UPDATE SET end=excluded.end,payload=excluded.payload "
                    "WHERE activities.end IS NULL",
                    (
                        run_id,
                        item["id"],
                        item["start"],
                        item.get("end"),
                        item["category"],
                        item["plugin"],
                        pack(item),
                    ),
                )
            if records:
                self.db.execute(
                    "DELETE FROM activities WHERE seq < (SELECT seq FROM activities ORDER BY seq DESC LIMIT 1 OFFSET 19999)"
                )

    def growth_samples(self, run_id: str, since: float, until: float):
        """Stream history; retain only endpoints, not thousands of process trees."""
        from .activity import number

        first = last = None
        peak, gap, count = None, 0, 0
        with self.lock:
            cursor = self.db.execute(
                "SELECT payload FROM samples WHERE run=? AND ts>=? AND ts<=? ORDER BY ts,id",
                (run_id, since, until),
            )
            for row in cursor:
                sample = unpack(row[0])
                value = number(sample.get("memory", {}).get("current"))
                if value is not None:
                    peak = value if peak is None else max(peak, value)
                if last is not None:
                    gap = max(gap, sample["ts"] - last["ts"])
                if first is None:
                    first = sample
                last = sample
                count += 1
        endpoints = [] if first is None else [first] if count == 1 else [first, last]
        return endpoints, {"peak": peak, "max_gap_seconds": gap, "sample_count": count}

    def activities(
        self,
        run_id: str,
        *,
        since=0,
        until=None,
        before=None,
        plugin="",
        category="",
        limit=50,
    ) -> dict:
        where, params = ["run=?", "COALESCE(end,start+1800)>=?"], [run_id, since]
        if until is not None:
            where.append("start<=?")
            params.append(until)
        if before is not None:
            where.append("seq<?")
            params.append(before)
        for field, value in (("plugin", plugin), ("category", category)):
            if value:
                where.append(f"{field}=?")
                params.append(value)
        limit = max(1, min(int(limit), 200))
        with self.lock:
            rows = self.db.execute(
                f"SELECT seq,payload FROM activities WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        items = [dict(unpack(row[1]), seq=row[0]) for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": items[-1]["seq"] if len(rows) > limit else None,
        }

    def save_job(self, job: dict) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?)",
                (job["id"], job["started_at"], pack(job)),
            )

    def jobs(self, limit=30) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT payload FROM jobs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [unpack(row[0]) for row in rows]

    def save_diagnostic(
        self, run_id: str, snapshot: dict, ts: float | None = None
    ) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO diagnostics VALUES (?,?,?)",
                (run_id, float(ts or time.time()), pack(snapshot)),
            )

    def diagnostic(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT ts, payload FROM diagnostics WHERE run=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return {"ts": row[0], "snapshot": unpack(row[1])}

    def latest_diagnostic(self) -> dict | None:
        """Return the newest saved snapshot, even before a fresh run exists."""

        with self.lock:
            row = self.db.execute(
                "SELECT run, ts, payload FROM diagnostics ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"run_id": row[0], "ts": row[1], "snapshot": unpack(row[2])}

    def prune(self, force=False) -> None:
        with self.lock:
            if not force and time.monotonic() - self._pruned < 60:
                return
            self._pruned = time.monotonic()
            cutoff = time.time() - self.retention_days * 86400
            keep = self.max_samples // 2 if force else self.max_samples
            with self.db:
                for table in ("samples", "events"):
                    self.db.execute(f"DELETE FROM {table} WHERE ts<?", (cutoff,))
                    self.db.execute(
                        f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?)",
                        (keep,),
                    )
                self.db.execute("DELETE FROM runs WHERE ts<?", (cutoff,))
                self.db.execute("DELETE FROM diagnostics WHERE ts<?", (cutoff,))
                self.db.execute("DELETE FROM activities WHERE start<?", (cutoff,))
                self.db.execute(
                    "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs ORDER BY ts DESC LIMIT -1 OFFSET 100)"
                )
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("PRAGMA incremental_vacuum(4096)")
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()
