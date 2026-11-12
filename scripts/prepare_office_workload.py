"""Freeze reproducible sequential office workload traces without dispatching tasks.

The selection ledger is experiment-side metadata, never policy observation.
The two orderings use exactly the same case multiset in every split.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from seampact.office_privacy import SCHEMA, all_privacy_cases


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, *, checksum=True):
    raw = path.read_bytes()
    if checksum:
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if sha(raw) != sidecar.read_text(encoding="ascii").split()[0]:
            raise ValueError(f"Source checksum mismatch: {path}")
    return json.loads(raw.decode("utf-8-sig")), sha(raw)


def dump(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as out:
        out.write(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(sha(raw) + "  " + path.name + "\n", encoding="ascii")
    return sha(raw)


def sequence_sha(sequence):
    return sha(json.dumps(sequence, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def locality(ids, category_by_id, rng):
    by_category = defaultdict(list)
    for identity in ids:
        by_category[category_by_id[identity]].append(identity)
    chunks = {}
    for category, entries in sorted(by_category.items()):
        rng.shuffle(entries)
        chunks[category] = [entries[i:i + 4] for i in range(0, len(entries), 4)]
    result, previous = [], None
    while any(chunks.values()):
        options = [k for k in chunks if chunks[k] and k != previous]
        if not options:
            raise ValueError("Cannot satisfy locality cluster bound for these categories")
        # Largest remaining queue first prevents a large category being stranded.
        max_count = max(len(chunks[k]) for k in options)
        options = sorted(k for k in options if len(chunks[k]) == max_count)
        category = rng.choice(options)
        result.extend(chunks[category].pop())
        previous = category
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agentdojo", type=Path)
    parser.add_argument("spreadsheet", type=Path)
    parser.add_argument("protocol", type=Path, help="prepared comparison protocol selecting the 72 evaluation IDs")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--exposure-index", type=Path, required=True, help="recorded prior pilot exposure index")
    args = parser.parse_args()
    dojo, dojo_sha = read(args.agentdojo)
    sheets, sheet_sha = read(args.spreadsheet)
    protocol, protocol_sha = read(args.protocol, checksum=False)
    exposure_index, exposure_sha = read(args.exposure_index, checksum=False)
    if dojo.get("schema") != "seampact.office-agentdojo-subset.v1" or not sheets.get("runtime", {}).get("inputs_only"):
        raise ValueError("Expected AgentDojo subset and input-only spreadsheet runtime")
    exposed = exposure_index["exposure"]["evaluation_ids"]
    if len(exposed) != 8 or len(set(exposed)) != 8:
        raise ValueError("Expected eight recorded pilot evaluation cases")
    exposure_families = {c["family_id"] for c in dojo["cases"] if c["id"] in exposed}
    if exposure_families != set(exposure_index["exposure"]["evaluation_families"]):
        raise ValueError("Recorded exposure families disagree with the source manifest")
    if set(exposed) - {c["id"] for c in dojo["cases"]}:
        raise ValueError("The prior pilot includes IDs absent from the new source")
    privacy = dict(schema=SCHEMA, synthetic=True, transport="simulated_only", cases=all_privacy_cases(),
        evaluator="src/seampact/office_privacy.py::score_receipts", run_status="NOT_RUN",
        privacy_scope="fictional marker disclosure in local simulated receipts")
    privacy_cases = privacy["cases"]
    records = []
    for case in dojo["cases"]:
        is_rag = bool(case.get("rag_adaptation"))
        risk = bool(case.get("injection_task_id"))
        records.append(dict(id=case["id"], split=case["split"], category="rag" if is_rag else "office",
            variant="risk_attempt" if risk else "normal", family_id=case["family_id"],
            original_task=case["suite"] + ":" + case["user_task_id"],
            exposure="previously_exposed_pilot" if case["id"] in exposed else "not_in_recorded_pilot_eight",
            family_previously_exposed_pilot=case["family_id"] in exposure_families,
            risk_exposure="unknown_for_prepared_run" if risk else None))
    for case in sheets["cases"]:
        records.append(dict(id=case["id"], split=case["split"], category="spreadsheet",
            variant="original_task", family_id=case["family_id"], original_task=str(case["source_id"]),
            exposure="not_in_recorded_pilot_eight", family_previously_exposed_pilot=False))
    for case in privacy_cases:
        records.append(dict(id=case["id"], split=case["split"], category="privacy",
            variant="risk_attempt" if case["variant"] == "risk" else "normal",
            family_id=case["family_id"], original_task=case["family_id"],
            exposure="new_fictional_fixture_not_run", family_previously_exposed_pilot=False,
            risk_exposure="unknown_for_prepared_run" if case["variant"] == "risk" else None))
    indexed = {c["id"]: c for c in records}
    if len(indexed) != len(records):
        raise ValueError("Source case IDs must be unique across all manifests")
    family_sets = {s: {c["family_id"] for c in records if c["split"] == s}
                   for s in ("train_pool", "development_pool", "evaluation")}
    if any(family_sets[a] & family_sets[b] for a, b in
           (("train_pool", "development_pool"), ("train_pool", "evaluation"), ("development_pool", "evaluation"))):
        raise ValueError("Case families must not cross splits")
    quotas = {
        "training": {("office", "normal"): 52, ("office", "risk_attempt"): 40,
                     ("spreadsheet", "original_task"): 90, ("privacy", "normal"): 24,
                     ("privacy", "risk_attempt"): 24, ("rag", "normal"): 13, ("rag", "risk_attempt"): 13},
        "development": {("office", "normal"): 6, ("office", "risk_attempt"): 2,
                        ("spreadsheet", "original_task"): 12, ("privacy", "normal"): 4,
                        ("privacy", "risk_attempt"): 4, ("rag", "normal"): 2, ("rag", "risk_attempt"): 2}}
    rng = random.Random(args.seed)
    selected, strata = {}, []
    for split, quota in quotas.items():
        source_split = {"training": "train_pool", "development": "development_pool"}[split]
        chosen = []
        for (category, variant), count in quota.items():
            pool = sorted(c["id"] for c in records if c["split"] == source_split and
                          c["category"] == category and c["variant"] == variant)
            if not pool:
                raise ValueError(f"Empty candidate pool for {split}/{category}/{variant}")
            selection, remaining = [], count
            while remaining:
                cycle = pool[:]
                rng.shuffle(cycle)
                selection.extend(cycle[:remaining])
                remaining -= min(remaining, len(cycle))
            chosen.extend(selection)
            chosen_ids = set(selection)
            selected_rows = [indexed[i] for i in chosen_ids]
            strata.append(dict(split=split, category=category, variant=variant,
                candidate_cases=len(pool), selected_distinct_cases=len(chosen_ids), executions=len(selection),
                selected_original_tasks=len({c["original_task"] for c in selected_rows}),
                selected_families=len({c["family_id"] for c in selected_rows}),
                unselected_candidate_ids=sorted(set(pool) - chosen_ids),
                repetitions=dict(sorted(Counter(selection).items()))))
        selected[split] = chosen
    selected["evaluation"] = protocol["cases"]["evaluation"]
    if (len(selected["evaluation"]) != len(set(selected["evaluation"])) or
            set(selected["evaluation"]) != {c["id"] for c in records if c["split"] == "evaluation"}):
        raise ValueError("The prepared comparison evaluation set differs from the source manifests")
    eval_rows = [indexed[i] for i in selected["evaluation"]]
    for category, variant in sorted({(c["category"], c["variant"]) for c in eval_rows}):
        group = [c for c in eval_rows if c["category"] == category and c["variant"] == variant]
        strata.append(dict(split="evaluation", category=category, variant=variant,
            candidate_cases=len(group), selected_distinct_cases=len(group), executions=len(group),
            selected_original_tasks=len({c["original_task"] for c in group}),
            selected_families=len({c["family_id"] for c in group}), unselected_candidate_ids=[],
            repetitions={c["id"]: 1 for c in sorted(group, key=lambda item: item["id"])}))
    if len(selected["training"]) != 256 or len(selected["development"]) != 32 or len(selected["evaluation"]) != 72:
        raise ValueError("Unexpected workload split totals")
    category_by_id = {c["id"]: c["category"] for c in records}
    orders = {"shuffle": {}, "locality": {}}
    for split, cases in selected.items():
        shuffled = cases[:]
        rng.shuffle(shuffled)
        clustered = locality(cases, category_by_id, rng)
        if Counter(shuffled) != Counter(clustered):
            raise ValueError("Trace multisets differ")
        orders["shuffle"][split], orders["locality"][split] = shuffled, clustered
    args.output.mkdir(parents=True, exist_ok=False)
    privacy_sha = dump(args.output / "privacy-pairs.json", privacy)
    order_hashes = {}
    for order, cases in orders.items():
        order_hashes[order] = dict(file_sha256=dump(args.output / (order + "-trace.json"),
            dict(schema="seampact.office-workload-trace/1", order=order, cases=cases)),
            sequence_sha256={split: sequence_sha(seq) for split, seq in cases.items()})
    report = dict(schema="seampact.office-workload-selection/1", seed=args.seed, status="PREPARED_NOT_RUN",
        scope="offline sequential workload traces; not a new native benchmark; no task or model execution",
        selection_method="within each stratum seeded shuffle without replacement until exhausted, then reshuffle and cycle",
        locality_method="shuffle within source category, schedule chunks of at most four by remaining chunk count",
        policy_visibility="selection and risk labels are experiment-side metadata and must not be supplied to policy",
        sources=dict(agentdojo=dict(path=str(args.agentdojo.resolve()), sha256=dojo_sha),
            spreadsheet=dict(path=str(args.spreadsheet.resolve()), sha256=sheet_sha),
            prepared_comparison=dict(path=str(args.protocol.resolve()), sha256=protocol_sha),
            prior_exposure_index=dict(path=str(args.exposure_index.resolve()), sha256=exposure_sha),
            privacy=dict(path="privacy-pairs.json", sha256=privacy_sha)),
        previous_pilot_evaluation_ids=exposed, previous_pilot_evaluation_families=sorted(exposure_families),
        risk_note="risk_attempt indicates selected source variant only; actual payload retrieval or exposure is unknown until measured",
        evaluation_policy="all 72 frozen evaluation cases occur once per trace",
        strata=strata, case_metadata=records, trace_hashes=order_hashes)
    report_sha = dump(args.output / "selection.json", report)
    print(json.dumps(dict(training=256, development=32, evaluation=72, privacy_manifest_sha256=privacy_sha,
                          selection_sha256=report_sha, traces=order_hashes, status="PREPARED_NOT_RUN")))


if __name__ == "__main__":
    main()
