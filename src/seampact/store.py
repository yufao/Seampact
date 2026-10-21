"""Core source, analysis, Unit and task state for the live observation panel."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
              kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(kind,id));
        """)

    def save(self, kind, identity, data, event=None):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?)",
                            (kind, identity, encoded))
        if event in {"analysis_started", "analysis_finished", "call_enabled", "started", "physically_retired"}:
            print(f"[SeamPact] {event} {identity}", flush=True)

    def create(self, kind, identity, data):
        """Create an immutable identity without replacing a previous run."""
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute("INSERT INTO documents VALUES (?,?,?)", (kind, identity, encoded))

    def get(self, kind, identity):
        with self.lock:
            row = self.db.execute("SELECT data FROM documents WHERE kind=? AND id=?",
                                  (kind, identity)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT data FROM documents WHERE kind=? ORDER BY rowid DESC",
                                   (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def events(self, after=0):
        return []

    def close(self):
        with self.lock:
            self.db.close()
