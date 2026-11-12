"""Prepare family-disjoint SpreadsheetBench train/dev inputs and private answers.

This is static preparation only. It never invokes a model, a task, or a scorer.
The runtime and grading directories must be separate siblings in a private root.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import tarfile

from freeze_spreadsheet_office import ARCHIVE_SHA, CODE_REVISION, DATA_REVISION, KINDS, PATTERNS


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(digest(raw) + "  " + path.name + "\n", encoding="ascii")
    return digest(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("evaluation_manifest", type=Path)
    parser.add_argument("output", type=Path, help="new private directory; never mount grading in the runtime")
    args = parser.parse_args()
    if digest(args.archive.read_bytes()) != ARCHIVE_SHA:
        raise ValueError("SpreadsheetBench source archive identity changed")
    eval_raw = args.evaluation_manifest.read_bytes()
    if digest(eval_raw) != args.evaluation_manifest.with_suffix(".json.sha256").read_text(encoding="ascii").split()[0]:
        raise ValueError("Frozen evaluation subset identity changed")
    evaluation = json.loads(eval_raw)
    if (evaluation["data_source"]["revision"] != DATA_REVISION or
            evaluation["scoring_source"]["revision"] != CODE_REVISION):
        raise ValueError("Evaluation source revisions do not match")
    evaluation_families = {row["family_id"] for row in evaluation["cases"] if row["split"] == "evaluation"}
    if len(evaluation_families) != 12:
        raise ValueError("Expected the twelve distinct frozen evaluation families")
    rng = random.Random(20261112)
    with tarfile.open(args.archive) as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        data = json.load(tar.extractfile("all_data_912_v0.1/dataset.json"))
        if len(data) != 912:
            raise ValueError("Expected the complete V1 source")
        def names(item):
            identity = str(item["id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
                raise ValueError("Unexpected source ID")
            root = f"all_data_912_v0.1/spreadsheet/{identity}/"
            return [root + f"{n}_{identity}_{kind}.xlsx" for n in (1, 2, 3) for kind in ("input", "answer")]
        chosen, families = [], set(evaluation_families)
        for category, pattern in PATTERNS.items():
            for kind in KINDS:
                eligible = sorted((x for x in data if x["instruction_type"] == kind
                    and re.search(pattern, x["instruction"], re.I)
                    and not re.search(r"\b(highlight|colou?r|formatting|font)\w*\b", x["instruction"], re.I)
                    and all(n in members and members[n].size <= 16 * 1024 * 1024 for n in names(x))),
                    key=lambda x: str(x["id"]))
                rng.shuffle(eligible)
                selected = []
                for item in eligible:
                    family = "spreadsheetbench:" + str(item["id"]).split("-", 1)[0]
                    if family not in families:
                        families.add(family)
                        selected.append(item)
                    if len(selected) == 10:
                        break
                if len(selected) != 10:
                    raise ValueError(f"Insufficient disjoint candidates for {category}/{kind}")
                chosen.extend((category, "development_pool" if i < 2 else "train_pool", item)
                              for i, item in enumerate(selected))
        args.output.mkdir(parents=True, exist_ok=False)
        runtime, grading = args.output / "runtime", args.output / "grading"
        runtime.mkdir(); grading.mkdir()
        runtime_cases, grading_cases = [], []
        for category, split, item in chosen:
            identity = str(item["id"])
            public = dict(id="spreadsheetbench:" + identity, source_id=item["id"],
                family_id="spreadsheetbench:" + identity.split("-", 1)[0], split=split,
                selection_category=category, instruction=item["instruction"],
                instruction_type=item["instruction_type"], answer_position=item["answer_position"],
                source_answer_sheet=item.get("answer_sheet"))
            inputs, answers = [], []
            for member in names(item):
                content = tar.extractfile(member).read()
                role = "input" if member.endswith("_input.xlsx") else "answer"
                base, records = (runtime, inputs) if role == "input" else (grading, answers)
                path = base / "workbooks" / identity / Path(member).name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                records.append(dict(path=path.relative_to(base).as_posix(), role=role, sha256=digest(content)))
            runtime_cases.append(dict(public, files=inputs, readiness="prepared_not_run"))
            grading_cases.append(dict(public, files=answers))
        # Preserve evaluation metadata and bytes exactly while splitting inputs
        # from the answers. The original frozen evaluation remains untouched.
        for original in evaluation["cases"]:
            if original["split"] != "evaluation":
                continue
            inputs, answers = [], []
            for record in original["files"]:
                source = args.evaluation_manifest.parent / record["path"]
                content = source.read_bytes()
                if digest(content) != record["sha256"]:
                    raise ValueError("Frozen evaluation workbook changed")
                role = record["role"]
                if role not in {"input", "answer"}:
                    raise ValueError("Unexpected evaluation workbook role")
                base, records = (runtime, inputs) if role == "input" else (grading, answers)
                path = base / record["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                records.append(dict(record))
            if len(inputs) != 3 or len(answers) != 3:
                raise ValueError("Evaluation must retain all three input/answer pairs")
            public = {key: value for key, value in original.items() if key != "files"}
            runtime_cases.append(dict(public, files=inputs))
            grading_cases.append(dict(public, files=answers))
    selection = dict(schema="seampact.spreadsheet-training-selection/1", seed=20261112,
        method="seeded within three keyword categories and two instruction types; per stratum 8 train and 2 dev; unique prefix family across all splits",
        evaluation_manifest_sha256=digest(eval_raw), archive_sha256=ARCHIVE_SHA,
        cases=[dict(id=row["id"], family_id=row["family_id"], split=row["split"],
                    selection_category=row["selection_category"], instruction_type=row["instruction_type"]) for row in runtime_cases])
    selection_sha = write_json(args.output / "selection.json", selection)
    common = dict(schema=evaluation["schema"], data_source=evaluation["data_source"],
                  scoring_source=evaluation["scoring_source"], selection_sha256=selection_sha)
    grading_sha = write_json(grading / "grading-manifest.json", dict(common, cases=grading_cases,
        grading=dict(answer_workbooks_only=True)))
    runtime_sha = write_json(runtime / "runtime-manifest.json", dict(common, cases=runtime_cases,
        source_manifest_sha256=grading_sha,
        runtime=dict(schema="seampact.office-spreadsheet-runtime/1", inputs_only=True,
                     answer_workbooks_included=False, status="PREPARED_NOT_RUN")))
    print(json.dumps(dict(training=48, development=12, evaluation=12, inputs=216, answers=216,
                          selection_sha256=selection_sha, runtime_sha256=runtime_sha,
                          grading_sha256=grading_sha, status="PREPARED_NOT_RUN")))


if __name__ == "__main__":
    main()
