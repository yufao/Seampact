"""Core source, analysis, Unit and task state for the live observation panel."""

import hashlib
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

    def save_runtime(self, session_id, state, *, gates=(), calls=(), roots=(), event=None):
        """Persist changed records; ordered IDs must follow their original insertion order."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("runtime session identity required")
        sections = {"gates": gates, "calls": calls, "roots": roots}
        meta = json.dumps({"keys": list(state), "values": {key: value for key, value in state.items()
                                                          if key not in sections}},
                          ensure_ascii=False, allow_nan=False)
        units, records = {}, []
        for section, changed in sections.items():
            if isinstance(changed, (set, frozenset)):
                raise TypeError("changed runtime identities must be ordered")
            identities = list(dict.fromkeys(changed))
            values = state[section]
            for identity in identities:
                record = dict(values[identity])
                unit_ref = None
                if section in {"gates", "calls"} and record.get("unit") is not None:
                    # This fixed encoding retains key order; different key orders
                    # are distinct versions, and identical representations share one.
                    unit_data = json.dumps(record["unit"], ensure_ascii=False, allow_nan=False,
                                           sort_keys=False, separators=(",", ":"))
                    unit_ref = hashlib.sha256(unit_data.encode("utf-8")).hexdigest()
                    units[unit_ref] = unit_data
                    record["unit"] = None  # Retain the field's insertion position.
                payload = json.dumps({"record": record, "unit_ref": unit_ref},
                                     ensure_ascii=False, allow_nan=False)
                records.append(("runtime_" + section + ":" + session_id, identity, payload))
        with self.transaction():
            self.db.executemany("INSERT OR IGNORE INTO documents VALUES ('runtime_unit',?,?)", units.items())
            self.db.executemany("""INSERT INTO documents VALUES (?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data""",
                [("runtime_meta", session_id, meta), *records])
        if event in {"call_enabled", "started", "physically_retired"}:
            print(f"[SeamPact] {event} local", flush=True)

    def read_runtime(self, session_id):
        """Reconstruct an isolated session's retained runtime state on demand."""
        with self.lock:
            meta_row = self.db.execute("SELECT data FROM documents WHERE kind='runtime_meta' AND id=?",
                                       (session_id,)).fetchone()
            if not meta_row:
                return None
            meta = json.loads(meta_row[0])
            sections = {}
            for section in ("gates", "calls", "roots"):
                rows = self.db.execute("SELECT id,data FROM documents WHERE kind=? ORDER BY rowid",
                                       ("runtime_" + section + ":" + session_id,)).fetchall()
                values = {}
                for identity, raw in rows:
                    payload = json.loads(raw)
                    record = payload["record"]
                    if payload["unit_ref"] is not None:
                        unit_row = self.db.execute("SELECT data FROM documents WHERE kind='runtime_unit' AND id=?",
                                                   (payload["unit_ref"],)).fetchone()
                        if not unit_row:
                            raise ValueError("missing retained runtime unit content")
                        record["unit"] = json.loads(unit_row[0])
                    values[identity] = record
                sections[section] = values
        return {key: sections[key] if key in sections else meta["values"][key] for key in meta["keys"]}

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

    # Task history. Polling and scheduling read active tasks and a bounded page
    # of summaries; a finished task's full record is read by identity on demand.

    _TASK_DETAIL = ("messages", "wire_messages", "context_messages", "request", "evidence", "transparent_decisions",
                    "scene_calls", "model_call_details", "costs", "harness_resources", "role_resources", "placements",
                    "events", "capability_pool")
    _TASK_SUMMARY = "json_remove(task.data, " + ", ".join("'$." + name + "'" for name in _TASK_DETAIL) + ")"
    _TASK_VISIBLE = """NOT EXISTS (SELECT 1 FROM documents AS hidden
                       WHERE hidden.kind='task_deleted' AND hidden.id=task.id)"""
    _TASK_CONVERSATION = "COALESCE(json_extract(task.data,'$.conversation_id'), task.id)"
    _TASK_FINISHED = "COALESCE(json_extract(task.data,'$.finished_at'), json_extract(task.data,'$.ended_at'))"

    def _task_indexes(self):
        """Indexes that keep these queries proportional to their result, not to all history."""
        if getattr(self, "_task_indexed", False):
            return
        with self.transaction():
            # An index's implicit rowid orders each kind by its latest save.
            self.db.execute("CREATE INDEX IF NOT EXISTS documents_kind ON documents(kind)")
            self.db.execute("""CREATE INDEX IF NOT EXISTS task_status
                ON documents(json_extract(data,'$.status')) WHERE kind='task'""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS task_conversation
                ON documents(COALESCE(json_extract(data,'$.conversation_id'), id)) WHERE kind='task'""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS task_project
                ON documents(COALESCE(json_extract(data,'$.project_id'), 'default')) WHERE kind='task'""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS task_finished
                ON documents(COALESCE(json_extract(data,'$.finished_at'), json_extract(data,'$.ended_at')))
                WHERE kind='task'""")
        self._task_indexed = True

    def task_summaries(self, limit, before=None):
        """A page of visible tasks, latest saved first, without their large detail fields.

        Returns the tasks and a cursor for the next older page, or None at the end.
        """
        self._task_indexes()
        older, values = ("", ()) if before is None else (" AND task.rowid < ?", (before,))
        with self.lock:
            rows = self.db.execute("SELECT task.rowid, " + self._TASK_SUMMARY + """ FROM documents AS task
                WHERE task.kind='task'""" + older + " AND " + self._TASK_VISIBLE + """
                ORDER BY task.rowid DESC LIMIT ?""", (*values, limit + 1)).fetchall()
        page = rows[:limit]
        return [json.loads(row[1]) for row in page], page[-1][0] if len(rows) > limit else None

    def tasks_with_status(self, *statuses, summary=True, visible=False):
        """Tasks currently in one of the given states; full records when summary is False."""
        self._task_indexes()
        marks = ",".join("?" for _ in statuses)
        with self.lock:
            rows = self.db.execute("SELECT task.rowid, " + (self._TASK_SUMMARY if summary else "task.data") + """
                FROM documents AS task INDEXED BY task_status
                WHERE task.kind='task' AND json_extract(task.data,'$.status') IN (""" + marks + ")"
                + (" AND " + self._TASK_VISIBLE if visible else ""), statuses).fetchall()
        return [json.loads(row[1]) for row in sorted(rows, reverse=True)]

    def task_counts(self):
        self._task_indexes()
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM documents WHERE kind='task'").fetchone()[0]
            running = self.db.execute("""SELECT COUNT(*) FROM documents AS task INDEXED BY task_status
                WHERE task.kind='task' AND json_extract(task.data,'$.status')='RUNNING'""").fetchone()[0]
        return dict(total=total, running=running)

    def tasks_finished_since(self, stamp):
        """Status, end time and latency of tasks whose recorded end is not before ``stamp``."""
        self._task_indexes()
        with self.lock:
            rows = self.db.execute("SELECT json_extract(task.data,'$.status'), " + self._TASK_FINISHED + """,
                json_extract(task.data,'$.latency_ms') FROM documents AS task INDEXED BY task_finished
                WHERE task.kind='task' AND """ + self._TASK_FINISHED + " >= ?", (stamp,)).fetchall()
        return [dict(status=row[0], finished_at=row[1], latency_ms=row[2]) for row in rows]

    def conversation_tasks(self, conversation_id, summary=False):
        """Tasks of one conversation, latest first, read one record at a time."""
        self._task_indexes()
        with self.lock:
            rows = self.db.execute("""SELECT task.rowid FROM documents AS task INDEXED BY task_conversation
                WHERE task.kind='task' AND """ + self._TASK_CONVERSATION + "=?", (conversation_id,)).fetchall()
        for (rowid,) in sorted(rows, reverse=True):
            with self.lock:
                row = self.db.execute("SELECT " + (self._TASK_SUMMARY if summary else "task.data")
                                      + " FROM documents AS task WHERE task.rowid=? AND task.kind='task'", (rowid,)).fetchone()
            if row:
                yield json.loads(row[0])

    def conversation_heads(self, project_id=None):
        """Per task, latest first: the few fields a conversation list and its search read."""
        self._task_indexes()
        before = None
        while True:
            older, values = ("", ()) if before is None else (" AND task.rowid < ?", (before,))
            if project_id is not None:
                older += " AND COALESCE(json_extract(task.data,'$.project_id'), 'default')=?"
                values += (project_id,)
            with self.lock:
                rows = self.db.execute("SELECT task.rowid, task.id, " + self._TASK_CONVERSATION + """,
                    json_extract(task.data,'$.origin'), json_extract(task.data,'$.phase'),
                    json_extract(task.data,'$.conversation_id') IS NOT NULL, json_extract(task.data,'$.status'),
                    json_extract(task.data,'$.started_at'), json_extract(task.data,'$.finished_at'),
                    json_extract(task.data,'$.messages'), json_extract(task.data,'$.response'),
                    COALESCE(json_extract(task.data,'$.project_id'), 'default')
                    FROM documents AS task WHERE task.kind='task'""" + older + """
                    ORDER BY task.rowid DESC LIMIT 64""", values).fetchall()
            if not rows:
                return
            before = rows[-1][0]
            for row in rows:
                yield dict(id=row[1], conversation=row[2], origin=row[3], phase=row[4], threaded=bool(row[5]),
                           status=row[6], started_at=row[7], finished_at=row[8], messages=row[9], response=row[10],
                           project_id=row[11])

    def run_summaries(self):
        """Scheduler runs as polled: totals stay, per-window series are read with the run's detail."""
        with self.lock:
            rows = self.db.execute("""SELECT json_remove(data, '$.joint.service_windows', '$.joint.route_windows',
                '$.joint.resource_windows') FROM documents WHERE kind='scheduler_run' ORDER BY rowid DESC""").fetchall()
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
