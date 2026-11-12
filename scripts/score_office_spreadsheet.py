"""Score three completed SpreadsheetBench V1 replays in a separate grading environment.

Inputs and answers belong to the frozen private subset. Only candidate outputs
from the same submitted solution go here. Recalculation uses the pinned
upstream converter on temporary copies, preserving the agent's deliverables.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

from freeze_spreadsheet_office import CODE_REVISION


def verified_module(root, relative, expected_sha):
    path = root / relative
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
        raise ValueError(f"Scorer source has changed: {relative}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score(manifest, case_id, checkout, outputs, soffice=None):
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.with_suffix(".json.sha256").read_text().split()[0]:
        raise ValueError("Frozen subset digest mismatch")
    subset = json.loads(raw)
    source = subset["scoring_source"]
    if source["revision"] != CODE_REVISION or len(outputs) != 3:
        raise ValueError("Expected frozen V1 scoring source and three replay outputs")
    case = next((row for row in subset["cases"] if row["id"] == case_id), None)
    if case is None:
        raise ValueError("Question is absent from the frozen subset")
    judge = verified_module(checkout, "evaluation/evaluation.py", source["files"]["evaluation/evaluation.py"])
    recalc = verified_module(checkout, "evaluation/open_spreadsheet.py", source["files"]["evaluation/open_spreadsheet.py"])
    soffice = soffice or recalc.find_libreoffice()
    answers = {Path(item["path"]).name: item for item in case["files"] if item["role"] == "answer"}
    values, reasons = [], []
    with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(sys.stderr):
        for index, output in enumerate(outputs, 1):
            record = answers[f"{index}_{case['source_id']}_answer.xlsx"]
            answer = manifest.parent / record["path"]
            if hashlib.sha256(answer.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("Frozen answer workbook changed")
            if not output.is_file():
                values.append(False)
                reasons.append("missing delivered replay workbook")
                continue
            copy = Path(folder) / f"replay-{index}.xlsx"
            shutil.copyfile(output, copy)
            if not soffice or not recalc.just_open_libreoffice(str(copy), soffice):
                values.append(None)
                reasons.append("recalculation unavailable or unsuccessful")
                continue
            try:
                passed, detail = judge.compare_workbooks(str(answer), str(copy), case["instruction_type"],
                                                        case["answer_position"])
            except Exception as exc:
                values.append(None)
                reasons.append(f"scoring failed: {type(exc).__name__}")
            else:
                values.append(bool(passed))
                reasons.append(detail or None)
    return dict(case_id=case_id, source_revision=CODE_REVISION, cases=values, reasons=reasons,
                soft=sum(values) / 3 if None not in values else None,
                hard=False if False in values else True if None not in values else None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("case_id")
    parser.add_argument("checkout", type=Path, help="pinned upstream scoring checkout")
    parser.add_argument("outputs", type=Path, nargs=3, help="produced workbooks in original testcase order")
    parser.add_argument("--soffice", help="headless LibreOffice binary in the grading environment")
    args = parser.parse_args()
    print(json.dumps(score(args.manifest, args.case_id, args.checkout, args.outputs, args.soffice)))
