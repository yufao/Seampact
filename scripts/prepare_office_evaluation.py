"""Prepare a static 80-case office dispatch index; never execute a case.

AgentHarm records contain static identities and scorer status only and are
explicitly non-dispatchable. No models, remote services or benchmark runners
are imported. The source manifests stay at their original private locations.
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from seampact.office_privacy import SCHEMA, privacy_cases


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8-sig")), sha(raw)


def relative(path):
    return Path(os.path.relpath(path.resolve(), ROOT)).as_posix()


def write(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".partial")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha(raw)


def prepare(agentdojo_path, spreadsheet_path, agentharm_path, pilot_path, output):
    inputs = dict(agentdojo=agentdojo_path, spreadsheetbench=spreadsheet_path, agentharm=agentharm_path)
    loaded = {name: read(path) for name, path in inputs.items()}
    pilot, pilot_sha = read(pilot_path)
    exposed = pilot["cases"]["evaluation"]
    if not isinstance(exposed, list) or len(exposed) != 8 or len(set(exposed)) != 8:
        raise ValueError("Expected exactly eight distinct previously exposed pilot evaluation IDs")
    dispatch, sources = [], []
    for name, expected_count in (("agentdojo", 44), ("spreadsheetbench", 12), ("agentharm", 8)):
        obj, identity = loaded[name]
        cases = [case for case in obj["cases"] if case.get("split") == "evaluation"]
        if len(cases) != expected_count or len({case["id"] for case in cases}) != expected_count:
            raise ValueError(name + " evaluation count or IDs changed")
        source = obj.get("source", obj.get("data_source", {}))
        scorer = obj.get("scoring_source", {})
        if name == "agentdojo":
            adapter = "native_environment_and_evaluator_wired; RAG adaptation additionally requires frozen corpus and retrieval binding"
            scorer = dict(repository=source.get("repository"), revision=source.get("revision"),
                          implementation="src/seampact/office_benchmark.py::AgentDojoOfficeTask.evaluate",
                          metrics=["utility", "attack_success"])
        elif name == "spreadsheetbench":
            adapter = "pending_solution_replay_formula_recalculation_and_native_scorer_connection"
        else:
            adapter = "static_inventory_only; tool_adapter_and_native_scorer_pending; execution_disabled"
        sources.append(dict(id=name, manifest=relative(inputs[name]), manifest_sha256=identity,
            manifest_schema=obj["schema"], selected_evaluation_count=len(cases), upstream=source,
            scoring_source=scorer, adapter_status=adapter,
            source_readiness_counts=dict(Counter(case.get("readiness", "unspecified") for case in cases))))
        for case in cases:
            row = dict(id=case["id"], source=name, source_manifest_sha256=identity,
                       family_id=case["family_id"], split="evaluation",
                       source_readiness=case.get("readiness"), native_scoring_status=adapter,
                       exposure="previously_exposed_pilot" if case["id"] in exposed else "not_in_recorded_pilot_eight",
                       dispatch_allowed=name == "agentdojo" and not case.get("rag_adaptation"), execution_status="NOT_RUN")
            if name == "agentdojo":
                row.update(environment="rag_adaptation" if case.get("rag_adaptation") else "native",
                           user_task_id=case["user_task_id"], suite=case["suite"],
                           attack_evaluation=bool(case.get("injection_task_id")))
                if case.get("rag_adaptation"):
                    row["dispatch_blocker"] = "RAG API is wired but requires explicit frozen corpus and verified project retrieval binding"
            elif name == "spreadsheetbench":
                row.update(workbook_pair_count=sum(item.get("role") == "input" for item in case["files"]),
                           dispatch_blocker="native output replay and formula recalculation not connected")
            else:
                # Never copy action contents, grading function names or target tools.
                row.update(kind=case["kind"], upstream_behavior_id=case["behavior_id"],
                           dispatch_blocker="AgentHarm slice is static-only and must not be executed")
            dispatch.append(row)
    indexed = {case["id"]: case for case in loaded["agentdojo"][0]["cases"]}
    exposed_families = {indexed[identity]["family_id"] for identity in exposed if identity in indexed}
    for row in dispatch:
        row["family_previously_exposed_pilot"] = row["family_id"] in exposed_families
    output.mkdir(parents=True, exist_ok=True)
    fixtures = dict(schema=SCHEMA, synthetic=True, transport="simulated_only", cases=privacy_cases(),
                    evaluator="src/seampact/office_privacy.py::score_receipts",
                    privacy_scope="literal fictional protected markers in delivered mock receipts; no real credentials or sending",
                    metrics=["legitimate_task_success", "privacy_leakage"], run_status="NOT_RUN")
    fixture_sha = write(output / "privacy-pairs.json", fixtures)
    sources.append(dict(id="office_privacy", manifest=relative(output / "privacy-pairs.json"),
        manifest_sha256=fixture_sha, manifest_schema=SCHEMA, selected_evaluation_count=16,
        upstream=dict(kind="new_fictional_fixture", version="1"),
        scoring_source=dict(implementation="src/seampact/office_privacy.py::score_receipts",
                            metrics=["legitimate_task_success", "privacy_leakage"]),
        adapter_status="explicit_privacy_api_wired_to_ordinary_project_host; local_simulated_receipts_only"))
    for case in fixtures["cases"]:
        dispatch.append(dict(id=case["id"], source="office_privacy", source_manifest_sha256=fixture_sha,
            family_id=case["family_id"], split="evaluation", variant=case["variant"],
            native_scoring_status="new_synthetic_receipt_scorer; not a native external benchmark",
            source_readiness=case["readiness"], exposure="new_fictional_fixture_not_run",
            family_previously_exposed_pilot=False, dispatch_allowed=True,
            runtime_entry="/api/experiments/office/privacy", execution_status="NOT_RUN"))
    if len(dispatch) != 80 or len({row["id"] for row in dispatch}) != 80:
        raise ValueError("The combined static inventory must contain exactly 80 unique IDs")
    exposure = dict(manifest=relative(pilot_path), manifest_sha256=pilot_sha, evaluation_ids=exposed,
        evaluation_families=sorted(exposed_families),
        IDs_absent_from_current_main_manifest=sorted(set(exposed) - set(indexed)),
        note="These evaluation IDs have already been observed in the small pilot; preserve exposure in all future analysis.")
    source_index = dict(schema="seampact.office-evaluation-source-index/1", sources=sources,
        exposure=exposure, total_cases=80, run_status="NOT_RUN",
        preparation_source=dict(path="scripts/prepare_office_evaluation.py", sha256=sha(Path(__file__).read_bytes())),
        privacy_scorer_source=dict(path="src/seampact/office_privacy.py",
                                  sha256=sha((ROOT / "src/seampact/office_privacy.py").read_bytes())))
    source_sha = write(output / "source-index.json", source_index)
    inventory = dict(schema="seampact.office-evaluation-dispatch/1", total_cases=80,
        counts=dict(Counter(row["source"] for row in dispatch)), source_index_sha256=source_sha,
        preparation_only=True, model_calls=0, experiment_runs=0, cases=dispatch,
        evaluation_contract=dict(preserve_reached_denominators=True, missing_scores=None,
            aggregate_across_native_metrics=False, exposed_pilot_ids=exposed,
            runtime_path="same ordinary Pi business host; source-specific isolated environment and evaluator remain required"))
    manifest_sha = write(output / "dispatch-manifest.json", inventory)
    return dict(total=80, counts=inventory["counts"], dispatch_manifest_sha256=manifest_sha,
                source_index_sha256=source_sha, privacy_fixture_sha256=fixture_sha, run_status="NOT_RUN")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentdojo", type=Path, required=True)
    parser.add_argument("--spreadsheet", type=Path, required=True)
    parser.add_argument("--agentharm", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.agentdojo, args.spreadsheet, args.agentharm, args.pilot, args.output), indent=2))


if __name__ == "__main__":
    main()
