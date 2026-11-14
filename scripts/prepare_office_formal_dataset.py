"""Prepare an enlarged local office dataset without executing any task.

Only original SpreadsheetBench questions are added here. Existing defensive
case manifests are referenced unchanged; no attack text is constructed. All
answers stay under spreadsheet/grading, separate from runtime inputs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import re
import tarfile

from freeze_spreadsheet_office import ARCHIVE_SHA, CODE_REVISION, DATA_REVISION, KINDS, PATTERNS


SPLITS = ("train_pool", "development_pool", "evaluation")
MAX_FILE_BYTES = 16 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path, *, sidecar=True):
    path = Path(path)
    raw = path.read_bytes()
    if sidecar:
        expected = path.with_suffix(path.suffix + ".sha256").read_text(encoding="ascii").split()[0]
        if digest(raw) != expected:
            raise ValueError(f"Checksum mismatch: {path.name}")
    return json.loads(raw.decode("utf-8-sig")), digest(raw)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as out:
        out.write(raw)
    checksum = digest(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(checksum + "  " + path.name + "\n", encoding="ascii")
    return checksum


def ref(path, checksum=None):
    path = Path(path)
    return dict(path=str(path.resolve()), sha256=checksum or digest(path.read_bytes()))


def normalized_instruction(text):
    """Conservative mechanical proxy, not a semantic independence guarantee."""
    text = text.casefold().replace("\u00a0", " ")
    text = re.sub(r"\b[a-z]{1,3}\$?\d+(?::\$?[a-z]{1,3}\$?\d+)?\b", " cellref ", text)
    text = re.sub(r"\b\d+(?:[.,]\d+)*\b", " number ", text)
    return " ".join(re.findall(r"[a-z]+|[\u4e00-\u9fff]+", text))


class Groups:
    def __init__(self, identities):
        self.parent = {identity: identity for identity in identities}

    def find(self, identity):
        while self.parent[identity] != identity:
            self.parent[identity] = self.parent[self.parent[identity]]
            identity = self.parent[identity]
        return identity

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def make_groups(data):
    """Unite source prefixes, normalized equality and high shingle overlap."""
    groups = Groups(str(row["id"]) for row in data)
    prefix_seen, text_seen, inverted = {}, {}, defaultdict(list)
    shingles_by_id, edges = {}, []
    for row in sorted(data, key=lambda row: str(row["id"])):
        identity = str(row["id"])
        prefix = identity.split("-", 1)[0]
        if prefix in prefix_seen:
            groups.union(identity, prefix_seen[prefix])
            edges.append(dict(a=identity, b=prefix_seen[prefix], reason="source_id_prefix"))
        prefix_seen.setdefault(prefix, identity)
        text = normalized_instruction(row["instruction"])
        if text in text_seen:
            groups.union(identity, text_seen[text])
            edges.append(dict(a=identity, b=text_seen[text], reason="normalized_instruction_equal"))
        text_seen.setdefault(text, identity)
        tokens = text.split()
        shingles = {tuple(tokens[i:i + 3]) for i in range(len(tokens) - 2)}
        # Short instructions are joined only by equality or source prefix.
        if len(tokens) >= 15 and len(shingles) >= 10:
            candidates = Counter(other for item in shingles for other in inverted[item])
            for other, common in sorted(candidates.items()):
                prior = shingles_by_id[other]
                similarity = common / len(shingles | prior)
                if similarity >= 0.85:
                    groups.union(identity, other)
                    edges.append(dict(a=identity, b=other, reason="instruction_token_trigram_jaccard",
                                      similarity=round(similarity, 8)))
            for item in sorted(shingles):
                inverted[item].append(identity)
            shingles_by_id[identity] = shingles
    members = defaultdict(list)
    for identity in sorted(groups.parent):
        members[groups.find(identity)].append(identity)
    family_for = {identity: "spreadsheetbench:group-" + min(identities)
                  for identities in members.values() for identity in identities}
    return family_for, list(members.values()), edges


def workbook_names(row):
    identity = str(row["id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise ValueError("Unexpected SpreadsheetBench source ID")
    root = f"all_data_912_v0.1/spreadsheet/{identity}/"
    return [root + f"{number}_{identity}_{role}.xlsx" for number in (1, 2, 3)
            for role in ("input", "answer")]


def prepare_spreadsheets(args, prior, prior_sha):
    archive_sha = digest(args.archive.read_bytes())
    if archive_sha != ARCHIVE_SHA:
        raise ValueError("SpreadsheetBench archive does not match the pinned public source")
    if (prior["data_source"]["revision"] != DATA_REVISION or
            prior["scoring_source"]["revision"] != CODE_REVISION):
        raise ValueError("Prior SpreadsheetBench source/scorer identity changed")
    runtime, grading = args.output / "spreadsheet/runtime", args.output / "spreadsheet/grading"
    runtime.mkdir(parents=True, exist_ok=False)
    grading.mkdir(parents=True, exist_ok=False)
    rng = random.Random(args.seed)
    with tarfile.open(args.archive) as tar:
        members = {row.name: row for row in tar.getmembers() if row.isfile()}
        dataset = json.load(tar.extractfile("all_data_912_v0.1/dataset.json"))
        if len(dataset) != 912 or len({str(row["id"]) for row in dataset}) != 912:
            raise ValueError("Expected all 912 unique original SpreadsheetBench questions")
        family_for, groups, edges = make_groups(dataset)
        old_ids = {str(row["source_id"]) for row in prior["cases"]}
        old_prefixes = {identity.split("-", 1)[0] for identity in old_ids}
        old_families = {family_for[identity] for identity in old_ids}
        eligible, excluded = defaultdict(list), []
        by_id = {str(row["id"]): row for row in dataset}
        for identity, row in sorted(by_id.items()):
            categories = [name for name, pattern in PATTERNS.items()
                          if re.search(pattern, row["instruction"], re.I)]
            reasons = []
            if family_for[identity] in old_families:
                reasons.append("prior_prepared_family_or_near_duplicate")
            if row["instruction_type"] not in KINDS:
                reasons.append("unsupported_instruction_type")
            if not categories:
                reasons.append("outside_office_keyword_scope")
            if re.search(r"\b(highlight|colou?r|formatting|font)\w*\b", row["instruction"], re.I):
                reasons.append("explicit_visual_formatting_not_scored_by_values")
            if any(name not in members or members[name].size > MAX_FILE_BYTES for name in workbook_names(row)):
                reasons.append("incomplete_or_oversize_original_three_pairs")
            if reasons:
                excluded.append(dict(source_id=identity, family_id=family_for[identity], reasons=reasons))
            else:
                eligible[family_for[identity]].append((categories[0], row))
        # One original per grouped family. Neither the three inputs nor
        # same-family alternate prompts count as independent originals.
        representatives, alternate_ids = [], []
        for family, candidates in sorted(eligible.items()):
            candidates.sort(key=lambda pair: str(pair[1]["id"]))
            representative = rng.choice(candidates)
            representatives.append((representative[0], representative[1]["instruction_type"], family, representative[1]))
            alternate_ids.extend(dict(source_id=str(row["id"]), family_id=family,
                                      reason="one_representative_per_grouped_family")
                                 for _, row in candidates if row is not representative[1])
        strata = defaultdict(list)
        for entry in representatives:
            strata[entry[:2]].append(entry)
        selected, stratum_counts = [], []
        for (category, kind), candidates in sorted(strata.items()):
            rng.shuffle(candidates)
            count = len(candidates)
            dev = max(1, round(count * args.holdout_fraction))
            evaluation = max(1, round(count * args.holdout_fraction))
            if dev + evaluation >= count:
                raise ValueError("Insufficient family count in spreadsheet stratum")
            for index, entry in enumerate(candidates):
                split = "development_pool" if index < dev else "evaluation" if index < dev + evaluation else "train_pool"
                selected.append((split, *entry))
            stratum_counts.append(dict(category=category, instruction_type=kind,
                                       train_pool=count-dev-evaluation, development_pool=dev, evaluation=evaluation))
        runtime_cases, grading_cases = [], []
        file_signatures = defaultdict(list)
        selected_names = {name for entry in selected for name in workbook_names(entry[-1])}
        file_records = {}
        # Gzip archives cannot seek cheaply. Read chosen members in their
        # archive order once, rather than repeatedly decompressing the source.
        for name in sorted(selected_names, key=lambda name: members[name].offset_data):
            content = tar.extractfile(members[name]).read()
            identity = name.split("/")[-2]
            role = "input" if name.endswith("_input.xlsx") else "answer"
            base = runtime if role == "input" else grading
            path = base / "workbooks" / identity / Path(name).name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            file_records[name] = dict(path=path.relative_to(base).as_posix(), role=role, sha256=digest(content))
        for split, category, kind, family, row in sorted(selected, key=lambda entry: (entry[0], str(entry[-1]["id"]))):
            identity = str(row["id"])
            public = dict(id="spreadsheetbench:" + identity, source_id=row["id"], family_id=family,
                          source_prefix_family_id="spreadsheetbench:" + identity.split("-", 1)[0], split=split,
                          selection_category=category, instruction=row["instruction"], instruction_type=kind,
                          answer_position=row["answer_position"], source_answer_sheet=row.get("answer_sheet"),
                          readiness="prepared_not_run")
            inputs, answers = [], []
            for name in workbook_names(row):
                record = file_records[name]
                (inputs if record["role"] == "input" else answers).append(record)
            signature = tuple(record["sha256"] for record in inputs + answers)
            file_signatures[signature].append(public["id"])
            runtime_cases.append(dict(public, files=inputs))
            grading_cases.append(dict(public, files=answers))
    if any(len(ids) > 1 for ids in file_signatures.values()):
        raise ValueError("Identical six-workbook tasks remain after family selection")
    selection = dict(schema="seampact.office-formal-spreadsheet-selection/1", status="PREPARED_NOT_RUN",
        seed=args.seed, holdout_fraction=args.holdout_fraction, archive_sha256=archive_sha,
        prior_runtime_manifest_sha256=prior_sha,
        source_questions=912, prior_prepared_originals=len(old_ids), prior_prefix_families=len(old_prefixes),
        prior_grouped_families=len(old_families), eligible_originals=sum(map(len, eligible.values())),
        selected_originals=len(runtime_cases), selected_grouped_families=len(eligible),
        family_method=dict(source_id_prefix=True, normalized_instruction_equality=True,
                           instruction_token_trigram_jaccard_threshold=0.85,
                           minimum_tokens_for_similarity=15, minimum_distinct_trigrams=10,
                           connected_components=True,
                           limitation="Mechanical near-duplicate proxy; semantic independence and unseen operation mechanisms are not established."),
        split_method="one seeded representative per family; seeded split per first matching keyword category and instruction type; dev/evaluation each round(stratum_size * holdout_fraction)",
        keyword_assignment="first matching category in retrieve, aggregate, edit order",
        strata=stratum_counts,
        cases=[{key: row[key] for key in ("id", "source_id", "family_id", "source_prefix_family_id", "split", "selection_category", "instruction_type")}
               for row in runtime_cases],
        family_groups=[dict(family_id=family_for[group[0]], source_ids=group,
                            prior_prepared=family_for[group[0]] in old_families) for group in groups],
        grouping_edges=edges, exclusions=excluded, unselected_same_family_alternates=alternate_ids,
        identical_six_workbook_selected_groups=0,
        counting="one source ID is one original; each original has 3 input/answer test cases; no repeated dispatches generated")
    selection_sha = save(args.output / "spreadsheet/selection.json", selection)
    common = dict(schema=prior["schema"], data_source=prior["data_source"], scoring_source=prior["scoring_source"],
                  selection_sha256=selection_sha)
    grading_sha = save(grading / "grading-manifest.json", dict(common, cases=grading_cases,
        grading=dict(answer_workbooks_only=True, status="PREPARED_NOT_RUN")))
    runtime_sha = save(runtime / "runtime-manifest.json", dict(common, cases=runtime_cases,
        source_manifest_sha256=grading_sha,
        runtime=dict(schema="seampact.office-spreadsheet-runtime/1", inputs_only=True,
                     answer_workbooks_included=False, status="PREPARED_NOT_RUN")))
    return selection, runtime_sha


def summarize(records):
    return dict(distinct_cases=len(records), original_tasks=len({row["original_task"] for row in records}),
                families=len({row["family_id"] for row in records}),
                normal_cases=sum(row["variant"] != "risk_attempt" for row in records),
                risk_cases=sum(row["variant"] == "risk_attempt" for row in records))


def assemble(args, prior_workload, dojo, privacy, sheets, sources):
    configuration = dict(schema="seampact.office-formal-finalization-config/1", seed=args.seed,
        holdout_fraction=args.holdout_fraction,
        inputs={name: ref(getattr(args, name)) for name in
                ("archive", "prior_workload", "prior_spreadsheet", "agentdojo", "privacy")},
        run_evidence=[ref(path) for path in args.run_evidence],
        rag_metadata=ref(args.rag_metadata) if args.rag_metadata else None,
        script=ref(Path(__file__)))
    config_sha = save(args.output / "finalization-config.json", configuration)
    sources["finalization_config"] = ref(args.output / "finalization-config.json", config_sha)
    actual = defaultdict(list)
    for identity in prior_workload.get("previous_pilot_evaluation_ids", []):
        actual[identity].append(dict(kind="recorded_prior_pilot_run", source="prior_workload.previous_pilot_evaluation_ids"))
    evidence_sources = []
    known_ids = {row["id"] for row in dojo["cases"] + privacy["cases"] + sheets["cases"]}
    for path in args.run_evidence:
        evidence, evidence_sha = load(path, sidecar=False)
        evidence_sources.append(ref(path, evidence_sha))
        for row in evidence.get("tasks", []):
            # A prepared protocol alone is not actual-run evidence.
            identity = row.get("case") or row.get("case_id")
            if identity in known_ids and row.get("status") and row.get("calls"):
                actual[identity].append(dict(kind="recorded_local_task_with_calls", source=str(path.resolve()),
                                             status=row["status"]))
    actual_families = {row["family_id"] for row in dojo["cases"] + privacy["cases"] if row["id"] in actual}
    records = []
    for source, cases in (("agentdojo", dojo["cases"]), ("privacy", privacy["cases"]), ("spreadsheetbench", sheets["cases"])):
        for case in cases:
            if source == "agentdojo":
                variant = "risk_attempt" if case.get("injection_task_id") else "normal"
                category = "rag" if case.get("rag_adaptation") else "office"
                original = "agentdojo:" + case["suite"] + ":" + case["user_task_id"]
            elif source == "privacy":
                variant = "risk_attempt" if case["variant"] == "risk" else "normal"
                category, original = "privacy", case["family_id"]
            else:
                variant, category, original = "original_task", "spreadsheet", "spreadsheetbench:" + str(case["source_id"])
            prior_prepared = source != "spreadsheetbench"
            novelty = ("historical_reference" if case["split"] == "evaluation" else "prior_prepared_training") if prior_prepared else "fresh_family"
            records.append(dict(id=case["id"], split=case["split"], source=source, category=category,
                variant=variant, family_id=case["family_id"], original_task=original,
                novelty_stratum=novelty, prior_prepared=prior_prepared,
                actual_run_status="recorded_run" if case["id"] in actual else "not_found_in_supplied_run_records",
                family_has_recorded_run=case["family_id"] in actual_families,
                risk_exposure="not_determined_by_preparation" if variant == "risk_attempt" else None))
    if args.rag_metadata:
        rag, rag_sha = load(args.rag_metadata)
        sources["independent_rag_metadata"] = ref(args.rag_metadata, rag_sha)
        sources["independent_rag"] = rag.get("sources", {})
        for raw in rag["cases"]:
            row = dict(raw)
            for field in ("id", "split", "family_id", "original_task", "variant"):
                if not row.get(field):
                    raise ValueError(f"Independent RAG metadata missing {field}")
            row.update(source=row.get("source", "office_rag"), category="rag", novelty_stratum="fresh_family",
                       prior_prepared=False, actual_run_status="not_found_in_supplied_run_records",
                       family_has_recorded_run=False)
            records.append(row)
    ids = [row["id"] for row in records]
    if len(ids) != len(set(ids)) or any(row["split"] not in SPLITS for row in records):
        raise ValueError("Duplicate IDs or unsupported dataset split")
    families = {split: {row["family_id"] for row in records if row["split"] == split} for split in SPLITS}
    originals = {split: {row["original_task"] for row in records if row["split"] == split} for split in SPLITS}
    for values in (families, originals):
        if any(values[a] & values[b] for i, a in enumerate(SPLITS) for b in SPLITS[:i]):
            raise ValueError("An original task or family crosses train/dev/evaluation")
    counts = {split: summarize([row for row in records if row["split"] == split]) for split in SPLITS}
    strata = []
    for key in sorted({(row["split"], row["source"], row["category"], row["novelty_stratum"]) for row in records}):
        rows = [row for row in records if (row["split"], row["source"], row["category"], row["novelty_stratum"]) == key]
        strata.append(dict(zip(("split", "source", "category", "novelty_stratum"), key), **summarize(rows)))
    exposure = dict(schema="seampact.office-formal-exposure-ledger/1", evidence_sources=evidence_sources,
        prior_workload=sources["prior_workload"], actual_runs=[dict(id=identity, evidence=events) for identity, events in sorted(actual.items())],
        distinction="Prior preparation is known pool membership; recorded_run means evidence in supplied records, not inferred from pool membership. Absence is not proof of never-run.",
        new_holdout_policy="All previously prepared spreadsheet groups and their mechanical near-duplicates are excluded from all new spreadsheet splits. Historical AgentDojo/privacy evaluation remains separately labelled; it is not fresh heldout.")
    exposure_sha = save(args.output / "exposure-ledger.json", exposure)
    manifest = dict(schema="seampact.office-formal-dataset/1", status="PREPARED_NOT_RUN", seed=args.seed,
        scope="Local data preparation only; no model, task, training, server or deployment executed.",
        sources=sources, exposure_ledger=ref(args.output / "exposure-ledger.json", exposure_sha),
        split_semantics=dict(train_pool="training candidates", development_pool="development candidates", evaluation="historical_reference and fresh_family must be scored with separate denominators"),
        counting="No dispatch trace is produced here. Original tasks, case variants, source test files and later execution counts are distinct units.",
        policy_visibility="Split, risk, source-family, exposure and grading metadata are experiment-side only.",
        splits={split: [row["id"] for row in records if row["split"] == split] for split in SPLITS},
        counts=counts, strata=strata, case_metadata=records,
        disjointness=dict(family_overlap=0, original_task_overlap=0, prior_spreadsheet_family_in_fresh=0),
        limitations=["Spreadsheet grouped families use deterministic source-prefix and lexical near-duplicate proxies; semantic independence is not guaranteed.",
                     "Existing office/privacy scenarios and synthetic RAG topics do not establish unseen mechanisms or environments.",
                     "Prepared inputs and answer isolation do not establish task solvability or native grader replay success.",
                     "Risk IDs denote existing defensive variants; actual exposure and outcomes require runtime evidence."])
    save(args.output / "dataset-manifest.json", manifest)
    lines = ["# 扩充办公正式数据（仅本地准备）", "", "状态：PREPARED_NOT_RUN。未运行任务、模型或训练。", "",
             "| 划分 | 不同案例 | 来源原题／合成主题 | 去重代理家族 | 普通 | 风险变体 |", "|---|---:|---:|---:|---:|---:|"]
    for split, row in counts.items():
        lines.append(f"| {split} | {row['distinct_cases']} | {row['original_tasks']} | {row['families']} | {row['normal_cases']} | {row['risk_cases']} |")
    evaluation_rows = [row for row in records if row["split"] == "evaluation"]
    lines.extend(["", "| 评价分母 | 不同案例 | 来源原题／合成主题 | 家族 |", "|---|---:|---:|---:|"])
    for novelty in ("fresh_family", "historical_reference"):
        row = summarize([row for row in evaluation_rows if row["novelty_stratum"] == novelty])
        lines.append(f"| {novelty} | {row['distinct_cases']} | {row['original_tasks']} | {row['families']} |")
    lines.extend(["", "`dataset-manifest.json` 是实验侧清单；不包含任务答案或攻击内容。运行输入见其 sources，表格答案仅在 spreadsheet/grading，不能挂入业务模型。",
        "", "评价同时保留历史 AgentDojo／隐私参照组，以及新家族组；两组须分开报告分母。旧表格72原题所在整族及其近重复不进入新分区。此前入池与有调用记录的实际运行分别登记；未找到记录不代表从未运行。",
        "", "每个表格原题保留3组原始输入／答案，它们不是3道独立原题。此入口不生成派发；重复执行也不增加原题。",
        "", "家族根据来源前缀、归一化题面相等及三词片段 Jaccard≥0.85 的连通分量划分，另核查所选六文件完整任务的相同摘要。该规则是可复建近重复代理，不保证所有语义近重复均被识别，也不代表新机制。",
        "", "来源、固定修订、校验和、排除理由、家族边和分层计数见 spreadsheet/selection.json。初次参数记录在 build-config.json，最终完整来源与脚本身份在 finalization-config.json；在新目录重跑同一参数，不改旧冻结材料。", ""])
    (args.output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("prior_workload", type=Path)
    parser.add_argument("prior_spreadsheet", type=Path)
    parser.add_argument("agentdojo", type=Path)
    parser.add_argument("privacy", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--holdout-fraction", type=float, default=0.15)
    parser.add_argument("--run-evidence", type=Path, action="append", default=[])
    parser.add_argument("--rag-metadata", type=Path)
    parser.add_argument("--spreadsheet-only", action="store_true", help="prepare sheet files now; finalize after optional RAG metadata is ready")
    parser.add_argument("--finalize", action="store_true", help="reuse checksum-verified sheet files in the output; write final dataset metadata")
    args = parser.parse_args()
    if not 0 < args.holdout_fraction < 0.4 or (args.finalize and args.spreadsheet_only):
        parser.error("Invalid fraction or mutually exclusive preparation mode")
    prior, prior_sha = load(args.prior_spreadsheet)
    prior_workload, workload_sha = load(args.prior_workload)
    dojo, dojo_sha = load(args.agentdojo)
    privacy, privacy_sha = load(args.privacy)
    if dojo.get("schema") != "seampact.office-agentdojo-subset.v1" or privacy.get("schema") != "seampact.office-privacy-pairs/1":
        raise ValueError("Unexpected existing defensive source manifest")
    if args.finalize:
        selection, _ = load(args.output / "spreadsheet/selection.json")
        sheets, runtime_sha = load(args.output / "spreadsheet/runtime/runtime-manifest.json")
        if (selection["seed"] != args.seed or selection.get("holdout_fraction", 0.15) != args.holdout_fraction or
                selection["prior_runtime_manifest_sha256"] != prior_sha):
            raise ValueError("Prepared spreadsheet identity differs from finalize inputs")
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        save(args.output / "build-config.json", dict(schema="seampact.office-formal-build-config/1", seed=args.seed,
            holdout_fraction=args.holdout_fraction, inputs={name: ref(getattr(args, name)) for name in
                ("archive", "prior_workload", "prior_spreadsheet", "agentdojo", "privacy")},
            run_evidence=[ref(path) for path in args.run_evidence], script=ref(Path(__file__))))
        selection, runtime_sha = prepare_spreadsheets(args, prior, prior_sha)
        sheets, _ = load(args.output / "spreadsheet/runtime/runtime-manifest.json")
    if args.spreadsheet_only:
        print(json.dumps(dict(selected_originals=selection["selected_originals"], strata=selection["strata"],
                              runtime_manifest_sha256=runtime_sha, status="PREPARED_NOT_RUN")))
        return
    sources = dict(agentdojo=ref(args.agentdojo, dojo_sha), privacy=ref(args.privacy, privacy_sha),
        prior_workload=ref(args.prior_workload, workload_sha), prior_spreadsheet=ref(args.prior_spreadsheet, prior_sha),
        spreadsheet_runtime=ref(args.output / "spreadsheet/runtime/runtime-manifest.json", runtime_sha),
        spreadsheet_grading=ref(args.output / "spreadsheet/grading/grading-manifest.json"),
        spreadsheet_selection=ref(args.output / "spreadsheet/selection.json"))
    manifest = assemble(args, prior_workload, dojo, privacy, sheets, sources)
    print(json.dumps(dict(status=manifest["status"], counts=manifest["counts"], strata=manifest["strata"])))


if __name__ == "__main__":
    main()
