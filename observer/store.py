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
                self.db.execute(
                    "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs ORDER BY ts DESC LIMIT -1 OFFSET 100)"
                )
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("PRAGMA incremental_vacuum(4096)")
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()
