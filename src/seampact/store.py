"""Core source, analysis, Unit and task state for the live observation panel."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
              kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(kind,id));
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self._transaction_depth == 0
            self._transaction_depth += 1
            try:
                if outer:
                    with self.db:
                        yield
                else:
                    yield
            finally:
                self._transaction_depth -= 1

    def save(self, kind, identity, data, event=None):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?)",
                            (kind, identity, encoded))
        if event in {"analysis_started", "analysis_finished", "call_enabled", "started", "physically_retired"}:
            print(f"[SeamPact] {event} {identity}", flush=True)

    def create(self, kind, identity, data):
        """Create an immutable identity without replacing a previous run."""
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        with self.transaction():
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

    def task_history(self):
        """Visible task records; retained execution evidence stays in task."""
        with self.lock:
            rows = self.db.execute("""
                SELECT task.data FROM documents AS task
                WHERE task.kind='task' AND NOT EXISTS (
                    SELECT 1 FROM documents AS hidden
                    WHERE hidden.kind='task_deleted' AND hidden.id=task.id)
                ORDER BY task.rowid DESC
            """).fetchall()
        return [json.loads(row[0]) for row in rows]

    def hide_tasks(self, identities):
        """Hide checked, finished records without removing their evidence."""
        stamp = now()
        with self.transaction():
            self.db.executemany("INSERT OR IGNORE INTO documents VALUES ('task_deleted',?,?)",
                [(identity, json.dumps(dict(id=identity, deleted_at=stamp), ensure_ascii=False))
                 for identity in identities])

    def analysis_summaries(self, source_ids):
        """Read visible history without decoding its full model conversations."""
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with self.lock:
            rows = self.db.execute("""
                SELECT json_remove(a.data, '$.messages', '$.provider_profile')
                FROM documents AS a
                LEFT JOIN documents AS r ON r.kind='request'
                    AND r.id=json_extract(a.data, '$.request_id')
                WHERE a.kind='analysis'
                    AND COALESCE(NULLIF(json_extract(a.data, '$.harness_id'), ''),
                                 json_extract(r.data, '$.source_id')) IN (""" + placeholders + ") ORDER BY a.rowid DESC",
                tuple(source_ids)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, kind, identity):
        with self.transaction():
            self.db.execute("DELETE FROM documents WHERE kind=? AND id=?", (kind, identity))

    def events(self, after=0):
        return []

    def close(self):
        with self.lock:
            self.db.close()
