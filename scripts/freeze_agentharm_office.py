"""Freeze four original AgentHarm public-test office behavior pairs.

Output is private evaluation metadata, never agent context. All behavior
variants share id_original and stay in the public-test partition; the input
data remains unchanged. No AgentHarm execution loop is used here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


DATA_REVISION = "e23b3fe60a0da9037314b88e5ee3a0c054970dad"
CODE_REVISION = "50b8b19c3d4765898fa2f35d125389be45af9c3c"
FAMILIES = {"49": "sensitive local data exfiltration", "96": "workplace reporting",
            "109": "coworker harassment", "32": "coworker rumor propagation"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("dataset", type=Path)
    p.add_argument("inspect_source", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    def git(*cmd):
        return subprocess.check_output(["git", "-C", str(args.inspect_source), *cmd], text=True).strip()
    if git("rev-parse", "HEAD") != CODE_REVISION or git("status", "--porcelain"):
        raise ValueError("Inspect Evals checkout must match the clean frozen commit")
    files = ["harmful_behaviors_test_public.json", "benign_behaviors_test_public.json",
             "harmful_behaviors_validation.json", "benign_behaviors_validation.json"]
    raw = {name: json.loads((args.dataset / "benchmark" / name).read_text(encoding="utf-8"))["behaviors"]
           for name in files}
    cases = []
    for origin, reason in FAMILIES.items():
        for kind in ("benign", "harmful"):
            matches = [v for v in raw[f"{kind}_behaviors_test_public.json"]
                       if v["id_original"] == origin and v["detailed_prompt"] and not v["hint_included"]]
            if len(matches) != 1:
                raise ValueError(f"Expected one detailed no-hint {kind} variant for family {origin}")
            item = matches[0]
            cases.append(dict(id=f"agentharm:{kind}:{item['id']}", split="evaluation", source_split="test_public",
                family_id=f"agentharm:{origin}", id_original=origin, behavior_id=item["id"],
                kind=kind, selection_reason=reason, target_functions=item["target_functions"],
                grading_function=item["grading_function"], readiness="pending_tool_adapter_and_native_scorer"))
        if any(v["id_original"] == origin for name in files[2:] for v in raw[name]):
            raise ValueError("An evaluation behavior family appears in the validation set")
    grouped = {}
    for v in raw["harmful_behaviors_validation.json"]:
        grouped.setdefault(v["id_original"], []).append(v["id"])
    result = dict(schema="seampact.office-agentharm-subset.v1",
        data_source=dict(repository="https://huggingface.co/datasets/ai-safety-institute/AgentHarm",
                         revision=DATA_REVISION,
                         files={"benchmark/" + name: sha(args.dataset / "benchmark" / name) for name in files}),
        scoring_source=dict(repository="https://github.com/UKGovernmentBEIS/inspect_evals", revision=CODE_REVISION,
                            version="9-B", caveat="The upstream README recommends 6-B; report the actual 9-B version.",
                            path="src/inspect_evals/agentharm",
                            requires=["inspect_ai-compatible executed tool transcript", "native synthetic tools",
                                      "configured refusal judge", "configured semantic judge where required"]),
        selection=dict(method="manual office-relevance; detailed no-hint variants", seed=20261111,
                       validation_families=grouped, note="Validation families are a pool, not selected office training cases."),
        cases=cases)
    raw_out = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as f:
        f.write(raw_out)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        hashlib.sha256(raw_out).hexdigest() + "  " + args.output.name + "\n", encoding="ascii")
    print("Frozen", len(cases), "AgentHarm cases:", args.output)


if __name__ == "__main__":
    main()
