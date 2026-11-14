"""Build a development-only extension of a frozen office dataset, offline."""
import argparse
import copy
import json
from pathlib import Path

from prepare_office_comparison import checked, write
from prepare_office_formal_dataset import SPLITS, summarize


SOURCES = {"office_ci": "office-ci", "office_summedits": "office-summedits",
           "office_routing": "office-routing", "office_access": "office-access",
           "office_links": "office-links"}


def merge(dataset_path, source_paths, output):
    base, base_sha = checked(dataset_path)
    if base.get("schema") != "seampact.office-formal-dataset/1":
        raise ValueError("Expected a frozen office dataset")
    result = copy.deepcopy(base)
    if not source_paths or not set(source_paths) <= set(SOURCES):
        raise ValueError("Provide at least one supported extension source")
    if any(key in result["sources"] for key in source_paths):
        raise ValueError("Dataset already contains an extension source")
    rows = result["case_metadata"]
    all_ids = {r["id"] for r in rows}
    all_originals = {r["original_task"] for r in rows}
    all_families = {r["family_id"] for r in rows}
    if len(all_ids) != len(rows):
        raise ValueError("Existing dataset contains duplicate IDs")
    summary = {}
    for key in source_paths:
        schema_part = SOURCES[key]
        runtime_path, grading_path = source_paths[key]
        runtime, runtime_sha = checked(runtime_path, sidecar=runtime_path.with_suffix(runtime_path.suffix + ".sha256").exists())
        grading, grading_sha = checked(grading_path, sidecar=grading_path.with_suffix(grading_path.suffix + ".sha256").exists())
        if (runtime.get("schema") != f"seampact.{schema_part}-runtime/1" or
                grading.get("schema") != f"seampact.{schema_part}-grading/1"):
            raise ValueError(f"Unexpected {key} source schema")
        cases, answers = runtime.get("cases"), grading.get("cases")
        if not isinstance(cases, list) or not cases or not isinstance(answers, list):
            raise ValueError(f"Missing {key} cases or grading cases")
        ids = [r.get("id") for r in cases]
        answer_ids = [r.get("id") for r in answers]
        originals = [r.get("original_task") for r in cases]
        families = [r.get("family_id") for r in cases]
        for name, values in (("id", ids), ("original_task", originals), ("family_id", families)):
            if any(not isinstance(v, str) or not v for v in values) or len(values) != len(set(values)):
                raise ValueError(f"{key} requires one distinct {name} per case")
        if len(answer_ids) != len(set(answer_ids)) or set(answer_ids) != set(ids):
            raise ValueError(f"{key} runtime and grading IDs differ")
        if set(ids) & all_ids or set(originals) & all_originals or set(families) & all_families:
            raise ValueError(f"{key} overlaps an existing ID, original or family")
        if any(r.get("split") not in ("development", "development_pool") for r in cases):
            raise ValueError(f"{key} contains a case without an explicit development split")
        answers_by_id = {answer["id"]: answer for answer in answers}
        new_rows = []
        for case in cases:
            # Link labels remain in the private grading file, never the runtime input.
            variant = answers_by_id[case["id"]].get("variant") if key == "office_links" else case.get("variant")
            if key == "office_links" and "variant" in case:
                raise ValueError("Link labels must not be exposed in runtime cases")
            if variant not in ("clean", "normal", "risk", "risk_attempt"):
                raise ValueError(f"{key} has an unsupported variant")
            variant = "risk_attempt" if variant in ("risk", "risk_attempt") else "normal"
            if key == "office_ci" and variant != "risk_attempt":
                raise ValueError("CI extension cases must be risk attempts")
            new_rows.append(dict(id=case["id"], split="development_pool", source=key,
                category=key, variant=variant, family_id=case["family_id"],
                original_task=case["original_task"], novelty_stratum="development_extension",
                prior_prepared=False, actual_run_status="not_found_in_supplied_run_records",
                family_has_recorded_run=False,
                risk_exposure="not_determined_by_preparation" if variant == "risk_attempt" else None))
        rows.extend(new_rows)
        result["splits"]["development_pool"].extend(ids)
        result["sources"][key] = dict(manifest=str(runtime_path.resolve()), manifest_sha256=runtime_sha,
            grading_manifest=str(grading_path.resolve()), grading_sha256=grading_sha)
        summary[key] = summarize(new_rows)
        all_ids.update(ids)
        all_originals.update(originals)
        all_families.update(families)
    for split in SPLITS:
        result["counts"][split] = summarize([r for r in rows if r["split"] == split])
    for key in source_paths:
        subset = [r for r in rows if r["source"] == key]
        result["strata"].append(dict(split="development_pool", source=key, category=key,
            novelty_stratum="development_extension", **summarize(subset)))
    result["development_extensions"] = dict(base_dataset_sha256=base_sha,
        sources=summary, isolation="ID, original and recorded family identifiers checked against all existing splits; semantic independence is not proven.")
    result["limitations"].append("Extension identifiers are disjoint; unrecorded semantic overlap and actual task or grader success remain unverified.")
    identity = write(output, result)
    return dict(sha256=identity, development_sources=summary, counts=result["counts"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    for name in SOURCES:
        cli = name.replace("_", "-")
        parser.add_argument(f"--{cli}-runtime", type=Path)
        parser.add_argument(f"--{cli}-grading", type=Path)
    args = parser.parse_args()
    source_paths = {}
    for name in SOURCES:
        runtime, grading = getattr(args, name + "_runtime"), getattr(args, name + "_grading")
        if bool(runtime) != bool(grading):
            parser.error(f"{name} requires both runtime and grading")
        if runtime:
            source_paths[name] = (runtime, grading)
    print(json.dumps(merge(args.dataset, source_paths, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
