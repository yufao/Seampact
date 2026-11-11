"""Read-only recorded runs; no legacy execution engine is imported."""
import json
import threading
from pathlib import Path
from seampact.capability_contract import digest

HISTORY_DETAIL = frozenset((
    "tasks", "transitions", "decisions", "roots", "resource_ops", "costs", "residency", "setup", "release",
    "estimates", "on_demand", "scoring", "counters", "task_ids", "unknown_outcomes", "evidence", "calls",
    "model_calls", "module_feedback", "reward_components", "final_response", "interrupted_event"))


CURVE_POINT = ("kind", "episode", "step", "reward", "return", "window_return", "eligible", "interactions",
               "mean_active_roots")


def history_summary(item):
    """One episode or window as a run view lists it; its full record stays in the run file."""
    value = {key: field for key, field in item.items() if key not in HISTORY_DETAIL}
    if "resource_ops" in item:
        value["resource_ops_count"] = len(item["resource_ops"])
    for key in ("feedback", "independent_feedback"):
        if isinstance(item.get(key), dict):
            value[key] = {name: item[key][name] for name in ("reward", "metrics") if name in item[key]}
    return value


class RunHistory:
    def __init__(self, home, store):
        self.store = store
        self.directory = Path(home) / "policy" / "runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.offsets = {}

    def snapshot(self):
        return dict(runs=self.store.run_summaries(), read_only=True, config=None,
                    active_checkpoint=None, options={}, readiness={})

    def record(self, rid):
        """A run's document, stop request and frozen manifest, without its episode records."""
        run = self.store.get("scheduler_run", rid)
        if not run:
            raise ValueError("没有这个运行；需要完整 run_id")
        result = dict(run)
        result["stop_request"] = self.store.get("scheduler_stop_request", rid)
        with self.lock:
            result["manifest"] = json.loads((self.directory / rid / "manifest.json").read_text(encoding="utf-8"))
        if digest(result["manifest"]) != run["config_digest"]:
            raise ValueError("运行清单内容身份不符")
        return result


    def _positions(self, rid, name):
        """Byte offsets of the complete records in a run file, extended from the last scan."""
        path = self.directory / rid / (name + ".jsonl")
        with self.lock:
            index = self.offsets.setdefault((rid, name), dict(starts=[], end=0))
            size = path.stat().st_size if path.exists() else 0
            if size < index["end"]:
                index.update(starts=[], end=0)
            if size > index["end"]:
                with path.open("rb") as stream:
                    stream.seek(index["end"])
                    position = index["end"]
                    for line in stream:
                        # An interrupted writer may leave a partial final record. Only
                        # newline-terminated records are complete observation material.
                        if not line.endswith(b"\n"):
                            break
                        if line.strip():
                            index["starts"].append(position)
                        position += len(line)
                    index["end"] = position
            return path, list(index["starts"])


    def records(self, rid, name, start=0):
        """Complete records of a run's history or curves from position ``start``."""
        path, starts = self._positions(rid, name)
        if start >= len(starts):
            return
        with path.open("rb") as stream:
            for position in starts[start:]:
                stream.seek(position)
                yield json.loads(stream.readline().decode("utf-8"))


    def detail(self, rid):
        """Everything recorded for a run, for export and replay."""
        result = self.record(rid)
        for key in ("history", "curves"):
            result[key] = list(self.records(rid, key))
        return result


    def progress(self, rid, history_after=0, curves_after=0):
        """What a run view draws, read incrementally.

        Returns the summaries of records after the given positions. Per-task
        calls, transitions and evidence stay in the files; detail() has them.
        """
        for value in (history_after, curves_after):
            if type(value) is not int or value < 0:
                raise ValueError("记录位置须为非负整数")
        result = self.record(rid)
        history = [history_summary(item) for item in self.records(rid, "history", history_after)]
        curves = [{key: item[key] for key in CURVE_POINT if key in item}
                  for item in self.records(rid, "curves", curves_after)]
        result.update(view="summary", history=history, curves=curves,
                      history_after=history_after, history_total=history_after + len(history),
                      curves_after=curves_after, curves_total=curves_after + len(curves))
        return result
