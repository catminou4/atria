"""SQLite-backed run state: stage checkpoints, events, dashboard queries.

A crashed run resumes at its last completed stage; completed stage payloads
are persisted so register is never executed twice for the same run.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

STAGES = ("mailbox", "register", "challenge", "key_extract", "persist")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    email TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    current_stage TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    failure_class TEXT,
    failure_reason TEXT,
    api_key_hash TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stages (
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (run_id, stage)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT,
    stage TEXT,
    kind TEXT NOT NULL,
    detail TEXT
);
"""


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def new_run(self, run_id: str, email: str | None = None) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO runs(run_id,email,status,current_stage,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (run_id, email, "running", STAGES[0], now, now),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def list_runs(self) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_current_stage(self, run_id: str, stage: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET current_stage=?, updated_at=? WHERE run_id=?",
                (stage, time.time(), run_id),
            )

    def set_email(self, run_id: str, email: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET email=?, updated_at=? WHERE run_id=?",
                (email, time.time(), run_id),
            )

    def complete_stage(self, run_id: str, stage: str, payload: dict[str, Any] | None = None) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO stages(run_id,stage,status,payload,ts)"
                " VALUES(?,?,?,?,?)",
                (run_id, stage, "done", json.dumps(payload or {}), now),
            )
            self._conn.execute(
                "UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id)
            )

    def stage_payload(self, run_id: str, stage: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT payload FROM stages WHERE run_id=? AND stage=? AND status='done'",
            (run_id, stage),
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def finish_run(
        self,
        run_id: str,
        status: str,
        failure_class: str | None = None,
        failure_reason: str | None = None,
        api_key_hash: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status=?, failure_class=?, failure_reason=?,"
                " api_key_hash=COALESCE(?, api_key_hash), updated_at=? WHERE run_id=?",
                (status, failure_class, failure_reason, api_key_hash, time.time(), run_id),
            )

    def mark_run_started(self, run_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET updated_at=? WHERE run_id=?", (time.time(), run_id)
            )

    def event(self, run_id: str | None, stage: str | None, kind: str, detail: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(ts,run_id,stage,kind,detail) VALUES(?,?,?,?,?)",
                (time.time(), run_id, stage, kind, detail),
            )

    # --- pacing queries -------------------------------------------------

    def last_run_start_ts(self) -> float | None:
        cur = self._conn.execute(
            "SELECT MAX(created_at) FROM runs WHERE status IN ('running','done','failed')"
        )
        val = cur.fetchone()[0]
        return float(val) if val is not None else None

    def runs_today(self) -> int:
        day_start = time.time() - (time.time() % 86400)
        cur = self._conn.execute("SELECT COUNT(*) FROM runs WHERE created_at>=?", (day_start,))
        return int(cur.fetchone()[0])

    # --- dashboard queries ----------------------------------------------

    def stage_breakdown(self) -> dict[str, dict[str, int]]:
        cur = self._conn.execute(
            "SELECT stage, status, COUNT(*) FROM stages GROUP BY stage, status"
        )
        out: dict[str, dict[str, int]] = {}
        for stage, status, n in cur.fetchall():
            out.setdefault(stage, {})[status] = n
        return out

    def failure_histogram(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT failure_reason, COUNT(*) FROM runs"
            " WHERE status='failed' AND failure_reason IS NOT NULL"
            " GROUP BY failure_reason ORDER BY COUNT(*) DESC"
        )
        return {r: n for r, n in cur.fetchall()}

    def challenge_first_pass_rate(self) -> float | None:
        """Share of solved challenges that passed on the first attempt."""
        cur = self._conn.execute(
            "SELECT detail FROM events WHERE kind IN ('challenge_solved','challenge_attempt')"
        )
        events = cur.fetchall()
        if not events:
            return None
        solved_first = solved_total = 0
        attempts_by_run: dict[str, int] = {}
        for (detail,) in events:
            try:
                data = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("solved"):
                solved_total += 1
                if data.get("attempt") == 1:
                    solved_first += 1
            attempts_by_run[data.get("run_id", "")] = 0
        if solved_total == 0:
            return None
        return solved_first / solved_total

    def events_iter(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        q = "SELECT ts,run_id,stage,kind,detail FROM events"
        args: tuple = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        cur = self._conn.execute(q + " ORDER BY ts", args)
        for ts, run_id, stage, k, detail in cur.fetchall():
            yield {"ts": ts, "run_id": run_id, "stage": stage, "kind": k, "detail": detail}
