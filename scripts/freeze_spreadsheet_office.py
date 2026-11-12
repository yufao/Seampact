"""Freeze twelve full SpreadsheetBench V1 questions and all original cases.

The public archive remains the source of truth. This extracts the selected
inputs and answers into a private directory, never into the agent workspace.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import tarfile


DATA_REVISION = "ab0b742b0fc95b946f212d80ac7771b5531272e4"
ARCHIVE_SHA = "9cf7228b54f1edcdd4b372eb736774adf29cb4f804c9920229bac6c154833399"
CODE_REVISION = "49b73a94775fb489063f60ca1865e3a650079a79"
PATTERNS = {"retrieve": r"\b(extract|find|lookup|match|search)\w*\b",
            "aggregate": r"\b(sum|count|average|total|calculat)\w*\b",
            "edit": r"\b(replace|delete|remove|sort|filter)\w*\b"}
KINDS = ("Cell-Level Manipulation", "Sheet-Level Manipulation")


def sha(content):
    return hashlib.sha256(content).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("code", type=Path)
    parser.add_argument("output", type=Path, help="new private output directory")
    args = parser.parse_args()
    if sha(args.archive.read_bytes()) != ARCHIVE_SHA:
        raise ValueError("Original V1 data archive does not match official LFS SHA256")
    def git(*cmd):
        return subprocess.check_output(["git", "-C", str(args.code), *cmd], text=True).strip()
    if git("rev-parse", "HEAD") != CODE_REVISION or git("status", "--porcelain"):
        raise ValueError("SpreadsheetBench evaluation code does not match the clean frozen revision")
    rng = random.Random(20261111)
    args.output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(args.archive) as tar:
        members = {m.name for m in tar.getmembers() if m.isfile()}
        dataset = json.load(tar.extractfile("all_data_912_v0.1/dataset.json"))
        if len(dataset) != 912:
            raise ValueError("Expected complete official V1 dataset")
        def members_for(item):
            identity = str(item["id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
                raise ValueError("Unexpected source task ID")
            root = f"all_data_912_v0.1/spreadsheet/{identity}/"
            return [root + f"{n}_{identity}_{kind}.xlsx" for n in (1, 2, 3) for kind in ("input", "answer")]
        selected, family_ids = [], set()
        for category, pattern in PATTERNS.items():
            for kind in KINDS:
                candidates = sorted((x for x in dataset if x["instruction_type"] == kind
                    and re.search(pattern, x["instruction"], re.I)
                    # Upstream's active scorer compares values, not cell formatting.
                    and not re.search(r"\b(highlight|colou?r|formatting|font)\w*\b", x["instruction"], re.I)
                    and all(name in members for name in members_for(x))), key=lambda x: str(x["id"]))
                rng.shuffle(candidates)
                found = []
                for item in candidates:
                    family = str(item["id"]).split("-", 1)[0]
                    if family not in family_ids:
                        found.append(item)
                        family_ids.add(family)
                    if len(found) == 2:
                        break
                if len(found) != 2:
                    raise ValueError(f"Insufficient eligible questions for {category}/{kind}")
                selected.extend((category, item) for item in found)
        cases = []
        for category, item in selected:
            identity = str(item["id"])
            folder = args.output / "workbooks" / identity
            folder.mkdir(parents=True)
            files = []
            for member in members_for(item):
                data = tar.extractfile(member).read()
                name = Path(member).name
                (folder / name).write_bytes(data)
                files.append(dict(path=f"workbooks/{identity}/{name}", sha256=sha(data),
                                  role="answer" if name.endswith("_answer.xlsx") else "input"))
            cases.append(dict(id=f"spreadsheetbench:{identity}", source_id=item["id"],
                family_id="spreadsheetbench:" + identity.split("-", 1)[0], split="evaluation",
                selection_category=category, instruction=item["instruction"],
                instruction_type=item["instruction_type"], answer_position=item["answer_position"],
                source_answer_sheet=item.get("answer_sheet"), files=files,
                readiness="pending_solution_replay_and_formula_recalculation"))
    out = dict(schema="seampact.office-spreadsheetbench-v1-subset.v1",
        data_source=dict(repository="https://huggingface.co/datasets/KAKA22/SpreadsheetBench",
                         revision=DATA_REVISION, archive_sha256=ARCHIVE_SHA, total_questions=912),
        scoring_source=dict(repository="https://github.com/RUCKBReasoning/SpreadsheetBench",
            revision=CODE_REVISION, files={p: sha((args.code / p).read_bytes())
            for p in ("evaluation/evaluation.py", "evaluation/open_spreadsheet.py")},
            note="Original scorer uses 3 cases and cached values; formula recalculation is required."),
        selection=dict(seed=20261111, method="seeded selection: 2 cell and 2 sheet questions per keyword category; exclude explicit visual-formatting tasks; require all 3 input/answer pairs; no repeated ID-prefix family",
                       train_policy="Evaluation families, including same-prefix IDs, excluded from development and training."),
        cases=cases)
    raw = (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (args.output / "manifest.json").write_bytes(raw)
    (args.output / "manifest.json.sha256").write_text(sha(raw) + "  manifest.json\n", encoding="ascii")
    print("Frozen", len(cases), "full V1 tasks, each with 3 input/answer pairs")


if __name__ == "__main__":
    main()
