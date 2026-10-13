"""Atomic checkpoints and an ordered event journal; never store credentials here."""

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
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
              kind TEXT NOT NULL, entity TEXT NOT NULL, data TEXT NOT NULL);
        """)

    def save(self, kind, identity, data, event=None):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?)",
                            (kind, identity, encoded))
            if event:
                self.db.execute("INSERT INTO events(at,kind,entity,data) VALUES(?,?,?,?)",
                                (now(), event, identity, encoded))

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
        with self.lock:
            rows = self.db.execute("SELECT seq,at,kind,entity,data FROM events WHERE seq>? ORDER BY seq",
                                   (after,)).fetchall()
        return [dict(seq=r[0], at=r[1], kind=r[2], entity=r[3], data=json.loads(r[4]))
                for r in rows]

    def close(self):
        with self.lock:
            self.db.close()
