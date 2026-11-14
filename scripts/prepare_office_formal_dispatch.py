"""Materialize the reviewed office corpus as matched open-loop arrival blocks, offline."""
import argparse
import copy
from collections import Counter
import json
from pathlib import Path
import random
from zipfile import ZipFile

from prepare_office_comparison import checked, write


WIDTH = 4
WAVE_GAP_MS = 6000
SPLITS = {"training": "train_pool", "development": "development_pool", "evaluation": "evaluation"}


def sheet_sizes(manifest_path, manifest):
    sizes = {}
    for case in manifest["cases"]:
        first = next(f for f in case["files"] if Path(f["path"]).name.startswith("1_") and f["role"] == "input")
        path = manifest_path.parent / first["path"]
        with ZipFile(path) as book:
            sizes[case["id"]] = sum(f.file_size for f in book.infolist()
                                    if f.filename.startswith("xl/worksheets/") and f.filename.endswith(".xml"))
    return sizes


def arrange(rows, sizes, rng):
    """Within-source size contrasts; other office cases rotate through sustained blocks."""
    sheets = [r["id"] for r in rows if r["category"] == "spreadsheet"]
    rng.shuffle(sheets)
    sheets.sort(key=sizes.__getitem__)
    quarter = (len(sheets) // 4 // WIDTH) * WIDTH
    if quarter < WIDTH:
        raise ValueError("Both ends of the sheet size distribution must fill a wave")
    light, heavy = sheets[:quarter], sheets[-quarter:]
    middle = sheets[quarter:-quarter]
    other = [r["id"] for r in rows if r["category"] not in {"rag", "spreadsheet"}]
    isolated = [r["id"] for r in rows if r["category"] == "rag"]
    for pool in (light, heavy, middle, other):
        rng.shuffle(pool)
    blocks = []

    def add(ids, profile, offsets):
        blocks.append((ids, dict(profile=profile, offsets_ms=offsets)))

    for pool, profile in ((light, "small_workbook_burst"), (heavy, "large_workbook_burst")):
        for i in range(0, len(pool), WIDTH):
            add(pool[i:i + WIDTH], profile, [0] * WIDTH)
    # Interleave workbooks with office and outward-delivery tasks across blocks.
    # Rate and spacing are fixed before execution and never depend on an arm's speed.
    combined = []
    total = len(middle) + len(other)
    other_count = len(other)
    for i in range(total):
        take_other = (i + 1) * other_count // total > i * other_count // total
        combined.append((other if take_other else middle).pop())
    for i in range(0, total, 4 * WIDTH):
        ids = combined[i:i + 4 * WIDTH]
        add(ids, "sustained_mixed" if len(ids) >= 2 * WIDTH else "mixed_tail",
            [(j // WIDTH) * WAVE_GAP_MS for j in range(len(ids))])
    for identity in isolated:
        add([identity], "exclusive_corpus", [0])
    rng.shuffle(blocks)
    ids = [identity for group, _ in blocks for identity in group]
    if Counter(ids) != Counter(r["id"] for r in rows):
        raise ValueError("Dispatch changed the selected case multiset")
    return ids, [block for _, block in blocks], dict(
        profiles=dict(Counter(block["profile"] for _, block in blocks)),
        small_workbook_max_xml_bytes=max(sizes[x] for x in light),
        large_workbook_min_xml_bytes=min(sizes[x] for x in heavy),
        total_arrivals=len(ids))


def extend_protocol(dataset, dataset_sha, base, base_sha, mapped):
    """Rearrange development only; retain the frozen train and evaluation dispatch."""
    extension = dataset.get("development_extensions")
    if (not extension or base.get("schema") != "seampact.office-comparison/1" or
            base.get("workload_trace", {}).get("order") != "timed_mixed" or
            base["workload_trace"].get("dataset_sha256") != extension["base_dataset_sha256"]):
        raise ValueError("Base protocol does not match the extension's frozen dataset")
    plan = copy.deepcopy(base)
    source_keys = tuple(extension["sources"])
    if not source_keys or not set(source_keys) <= {"office_ci", "office_summedits", "office_routing", "office_access", "office_links"}:
        raise ValueError("Unsupported or empty extension sources")
    rows = [r for r in dataset["case_metadata"] if r["source"] in source_keys]
    if any(r["split"] != "development_pool" for r in rows):
        raise ValueError("Extension cannot add training or evaluation cases")
    ids = [r["id"] for r in rows]
    prior = {identity for values in base["cases"].values() for identity in values}
    if len(ids) != len(set(ids)) or prior.intersection(ids):
        raise ValueError("Extension IDs overlap the prior dispatch")
    for split, pool in SPLITS.items():
        expected = [r["id"] for r in dataset["case_metadata"]
                    if r["split"] == pool and r["source"] not in source_keys]
        if Counter(base["cases"][split]) != Counter(expected):
            raise ValueError(f"Base {split} dispatch does not match the original dataset")
    # All paths in the existing protocol are local inputs; verify their identities
    # before rebasing them for the chosen execution host.
    _, identity = checked(Path(plan["manifest"]))
    if identity != plan["manifest_sha256"]:
        raise ValueError("AgentDojo source identity differs from the base protocol")
    plan["manifest"] = mapped(plan["manifest"])
    for key, ref in plan["source_manifests"].items():
        for path_key, sha_key in (("manifest", "manifest_sha256"), ("grading_manifest", "grading_sha256")):
            if path_key in ref:
                path = Path(ref[path_key])
                _, identity = checked(path, sidecar=path.with_suffix(path.suffix + ".sha256").exists())
                if identity != ref[sha_key]:
                    raise ValueError(f"{key} source identity differs from the base protocol")
                ref[path_key] = mapped(ref[path_key])
    corpus_path = Path(plan["rag"]["corpus_dir"])
    _, identity = checked(corpus_path / "corpus.json")
    if identity != plan["rag"]["corpus_sha256"]:
        raise ValueError("AgentDojo retrieval corpus identity differs from the base protocol")
    plan["rag"]["corpus_dir"] = mapped(corpus_path)
    for key in source_keys:
        ref = dataset["sources"][key]
        for path_key, sha_key in (("manifest", "manifest_sha256"), ("grading_manifest", "grading_sha256")):
            path = Path(ref[path_key])
            _, identity = checked(path, sidecar=path.with_suffix(path.suffix + ".sha256").exists())
            if identity != ref[sha_key]:
                raise ValueError(f"{key} source identity differs from the dataset")
        plan["source_manifests"][key] = dict(ref, manifest=mapped(ref["manifest"]),
                                                   grading_manifest=mapped(ref["grading_manifest"]))
    sheets_ref = dataset["sources"]["spreadsheet_runtime"]
    sheets_path = Path(sheets_ref["path"])
    sheets, identity = checked(sheets_path)
    if identity != sheets_ref["sha256"]:
        raise ValueError("Worksheet manifest differs from the selected corpus")
    development_rows = [r for r in dataset["case_metadata"] if r["split"] == "development_pool"]
    dev_ids, dev_blocks, dev_details = arrange(development_rows, sheet_sizes(sheets_path, sheets),
        random.Random(f"{dataset['seed']}:development"))
    plan["cases"]["development"] = dev_ids
    plan["arrival_schedule"]["development"] = dev_blocks
    trace = plan["workload_trace"]
    trace["dataset_sha256"] = dataset_sha
    trace["base_protocol_sha256"] = base_sha
    trace["splits"]["development"] = dev_details
    trace.setdefault("development_extensions", {}).update(copy.deepcopy(extension["sources"]))
    plan["quality_evaluator"] += "; development extensions scored by their source-specific graders"
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("base", type=Path, help="host-specific project, model, capability and policy configuration")
    parser.add_argument("output", type=Path)
    parser.add_argument("--agentdojo-corpus", type=Path,
                        help="directory containing the pinned AgentDojo corpus.json")
    parser.add_argument("--extend-protocol", action="store_true",
                        help="base is a frozen host-specific timed protocol; append only development extensions")
    parser.add_argument("--data-root", type=Path,
                        help="common local parent of all source directories when preparing container paths")
    parser.add_argument("--service-root", help="container path corresponding to --data-root")
    args = parser.parse_args()
    if bool(args.data_root) != bool(args.service_root):
        parser.error("Path mapping requires both --data-root and --service-root")
    def mapped(value):
        path = Path(value).resolve()
        if args.data_root:
            return args.service_root.rstrip("/") + "/" + path.relative_to(args.data_root.resolve()).as_posix()
        return str(path)
    dataset, dataset_sha = checked(args.dataset)
    base, base_sha = checked(args.base, sidecar=args.base.with_suffix(args.base.suffix + ".sha256").exists())
    if args.extend_protocol:
        plan = extend_protocol(dataset, dataset_sha, base, base_sha, mapped)
        print(json.dumps(dict(sha256=write(args.output, plan),
                              development_sources=dataset["development_extensions"]["sources"]), ensure_ascii=False))
        return
    if dataset.get("development_extensions"):
        parser.error("For an extended development dataset, use --extend-protocol with its frozen base protocol")
    if not args.agentdojo_corpus:
        parser.error("--agentdojo-corpus is required when preparing a new protocol")
    sources = dataset["sources"]
    def location(key):
        ref = sources[key]
        return dict(manifest=mapped(ref["path"]), manifest_sha256=ref["sha256"])
    sheets_ref = sources["spreadsheet_runtime"]
    sheets_path = Path(sheets_ref["path"])
    sheets, sheets_sha = checked(sheets_path)
    if sheets_sha != sheets_ref["sha256"]:
        raise ValueError("Worksheet manifest differs from the selected corpus")
    sizes = sheet_sizes(sheets_path, sheets)
    config_keys = ("project_id", "rag", "scheduler", "policies", "deadline_s", "resource_source", "learner_config")
    plan = {key: base[key] for key in config_keys}
    # Rebind a retained corpus to the native (corrected) offline artifact, even
    # when the template contains paths belonging to another deployment.
    rag_dir = args.agentdojo_corpus
    corpus, corpus_sha = checked(rag_dir / "corpus.json")
    if corpus["source_manifest_sha256"] != sources["agentdojo"]["sha256"]:
        raise ValueError("Retained office retrieval corpus does not match AgentDojo")
    plan["rag"].update(corpus_dir=mapped(rag_dir), corpus_sha256=corpus_sha)
    rag = dict(sources["independent_rag"]["office_rag"])
    for key in ("manifest", "grading_manifest"):
        rag[key] = mapped(rag[key])
    plan.update(schema="seampact.office-comparison/1", **location("agentdojo"), continuous_training=True,
        update_after_tasks=16, seed=dataset["seed"], arrival_width=WIDTH,
        source_manifests=dict(privacy=location("privacy"), spreadsheet=location("spreadsheet_runtime"), office_rag=rag),
        quality_evaluator="AgentDojo native safe utility; SpreadsheetBench three-replay hard score; "
            "simulated authorized delivery; synthetic RAG exact short-answer score",
        development_selector="scheduler", cases={}, arrival_schedule={}, prebuild=[])
    plan["scheduler"].update(elastic_actions=True, max_replicas_per_group=2, max_in_flight=4, objective=None)
    plan["policies"]["app_config"].update(scale_when_saturated=False)
    plan["policies"]["resource_rule"].update(scale_when_saturated=True)
    details = {}
    for split, pool in SPLITS.items():
        rows = [r for r in dataset["case_metadata"] if r["split"] == pool]
        rng = random.Random(f"{dataset['seed']}:{split}")
        ids, blocks, details[split] = arrange(rows, sizes, rng)
        plan["cases"][split] = ids
        plan["arrival_schedule"][split] = blocks
        plan["prebuild"].extend(r["id"] for r in rows if r["category"] == "rag")
    plan["workload_trace"] = dict(order="timed_mixed", dataset_sha256=dataset_sha, template_sha256=base_sha,
        width=WIDTH, interwave_ms=WAVE_GAP_MS, splits=details,
        size_proxy="uncompressed worksheet XML bytes in first visible workbook; confirm actual service duration in reports",
        capacity="up to sixteen overlapping business arrivals per block; at most four active capability calls")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(dict(sha256=write(args.output, plan), splits=details), ensure_ascii=False))


if __name__ == "__main__":
    main()
