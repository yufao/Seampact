"""A narrow runtime guard for Python threads beyond the admitted host work.

It never proves native threads, CUDA, async tasks or remote work have ended;
those remain separate source/SDK obligations. No thread-name allowlist is used.
"""
import threading

GUARD = "PYTHON_THREADS_QUIESCENT"
SCHEMA = "python-thread-census/1"


def census(known_threads):
    known = {id(thread) for thread in known_threads}
    # enumerate() includes threads still starting. Do not call user-overridable
    # name/is_alive/equality methods on a source-created Thread subclass.
    untracked = sum(id(thread) not in known for thread in threading.enumerate())
    return dict(schema=SCHEMA, status="QUIESCENT" if untracked == 0 else "PENDING",
                untracked_count=untracked, observation="POINT_IN_TIME",
                not_checked=["native_threads", "CUDA", "async_tasks", "remote_work"])


def quiescent(report):
    return (type(report) is dict and report.get("schema") == SCHEMA
            and report.get("status") == "QUIESCENT"
            and type(report.get("untracked_count")) is int and report["untracked_count"] == 0)


def required(contract):
    checks = (contract.get("lifecycle") or {}).get("checks", {})
    return any(checks.get(name, {}).get("status") == "SUPPORTED"
               and GUARD in checks[name].get("guards", []) for name in ("owner_end", "cleanup"))


def cleanup_obligations(contract):
    """Expose bound cleanup gaps without turning facts into callable actions.

    An idle monitor can be the only extra Python thread while cleanup is still
    illegal: its creators or another owner may remain live. No package name,
    candidate flag or point-in-time census discharges that source obligation.
    Current conditional facts do not contain a complete executable summary.
    """
    obligations = []
    evidence = (contract.get("lifecycle") or {}).get("source_evidence") or {}
    for member in evidence.get("members", []):
        for fact in (member.get("effect_evidence") or {}).get("facts", []):
            action = fact.get("cleanup")
            if type(action) is dict:
                obligations.append(dict(entry_id=member["entry_id"], fact_id=fact["id"],
                    rule=action.get("rule"), status="UNKNOWN", executable=False,
                    guards=list(action.get("guards", [])),
                    reason="partial original-call facts do not close creators, loaded object identity and runtime users"))
    return dict(schema="sdk-cleanup-obligations/1", status="UNKNOWN" if obligations else "NOT_APPLICABLE",
                executable=False, obligations=obligations)
