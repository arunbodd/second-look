"""SQLite persistence: cached AI assessments plus an append-only event log.

The event log is the audit trail. Every human action (decision, note, feedback, question) and
every AI answer is an event, so the current state of a case is derived by replaying its events.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS assessments (
    case_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (case_id, cache_key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    actor TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_case ON events (case_id, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connect()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _write(self, sql: str, params: tuple) -> int | None:
        """Run one write; if the database file was removed while running, reconnect once and retry."""
        with self._lock:
            for attempt in range(2):
                try:
                    cur = self._conn.execute(sql, params)
                    self._conn.commit()
                    return cur.lastrowid
                except sqlite3.OperationalError:
                    if attempt == 1 or self.path.exists():
                        raise
                    self._conn.close()
                    self._connect()
        return None

    # ---- assessments -----------------------------------------------------------------
    def get_assessment(self, case_id: str, cache_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM assessments WHERE case_id=? AND cache_key=?", (case_id, cache_key)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_assessment(self, case_id: str, cache_key: str, payload: dict) -> None:
        self._write(
            "INSERT OR REPLACE INTO assessments (case_id, cache_key, payload, created_at) VALUES (?,?,?,?)",
            (case_id, cache_key, json.dumps(payload), now_iso()),
        )

    def clear_assessments(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM assessments")
            self._conn.commit()

    # ---- events ----------------------------------------------------------------------
    def add_event(self, case_id: str, type_: str, payload: dict, actor: str = "system") -> dict:
        ts = now_iso()
        event_id = self._write(
            "INSERT INTO events (case_id, type, payload, actor, ts) VALUES (?,?,?,?,?)",
            (case_id, type_, json.dumps(payload), actor, ts),
        )
        return {"id": event_id, "case_id": case_id, "type": type_, "payload": payload, "actor": actor, "ts": ts}

    def events(self, case_id: str | None = None) -> list[dict]:
        with self._lock:
            if case_id is None:
                rows = self._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM events WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [
            {"id": r["id"], "case_id": r["case_id"], "type": r["type"], "payload": json.loads(r["payload"]),
             "actor": r["actor"], "ts": r["ts"]}
            for r in rows
        ]

    def clear_events(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events")
            self._conn.commit()
