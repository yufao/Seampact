"""Create a continuous-service protocol and optional separated transfer archives.

Preparation is offline: no service, model, task or policy is started.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def checked(path, sidecar=True):
    raw = path.read_bytes()
    identity = sha(raw)
    if sidecar and identity != path.with_suffix(path.suffix + ".sha256").read_text(encoding="ascii").split()[0]:
        raise ValueError(f"Identity changed: {path}")
    return json.loads(raw.decode("utf-8-sig")), identity


def write(path, obj):
    raw = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
    digest = sha(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(digest + "  " + path.name + "\n", encoding="ascii")
    return digest


def package(root, output, members):
    with tarfile.open(output, "x:gz") as tar:
        for name in members:
            directory = root / name
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError(f"Required package directory unavailable: {directory}")
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Symlinks are not allowed in transfer archives")
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    digest = sha(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("preparation_root", type=Path)
    parser.add_argument("--server-root", required=True, help="absolute POSIX runtime data path")
    parser.add_argument("--order", required=True, choices=("shuffle", "locality"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, help="optionally create runtime and separate grading tarballs")
    args = parser.parse_args()
    remote = PurePosixPath(args.server_root)
    if not remote.is_absolute() or ".." in remote.parts:
        raise ValueError("server-root must be an absolute POSIX path without parent traversal")
    root = args.preparation_root
    base, base_sha = checked(args.base, sidecar=False)
    selection, selection_sha = checked(root / "dispatch/selection.json")
    trace, trace_sha = checked(root / f"dispatch/{args.order}-trace.json")
    dojo, dojo_sha = checked(root / "agentdojo/manifest.json")
    rag, rag_sha = checked(root / "agentdojo/rag-corpus/corpus.json")
    spreadsheet, sheet_sha = checked(root / "spreadsheet/runtime/runtime-manifest.json")
    privacy, privacy_sha = checked(root / "dispatch/privacy-pairs.json")
    grading_path = root / "spreadsheet/grading/grading-manifest.json"
    _, grading_sha = checked(grading_path)
    if (selection["sources"]["agentdojo"]["sha256"] != dojo_sha or
            selection["sources"]["spreadsheet"]["sha256"] != sheet_sha or
            selection["sources"]["privacy"]["sha256"] != privacy_sha or
            selection["sources"]["prepared_comparison"]["sha256"] != base_sha or
            selection["trace_hashes"][args.order]["file_sha256"] != trace_sha or
            rag["source_manifest_sha256"] != dojo_sha or
            spreadsheet["source_manifest_sha256"] != grading_sha):
        raise ValueError("Source, trace, corpus, or grading identities do not match")
    if trace.get("order") != args.order or set(trace["cases"]) != {"development", "training", "evaluation"}:
        raise ValueError("Unexpected workload trace")
    cases = trace["cases"]
    if [len(cases[k]) for k in ("development", "training", "evaluation")] != [32, 256, 72]:
        raise ValueError("Unexpected workload size")
    all_cases = dojo["cases"] + spreadsheet["cases"] + privacy["cases"]
    indexed = {c["id"]: c for c in all_cases}
    if len(indexed) != len(all_cases):
        raise ValueError("Duplicate source case IDs")
    for split, values in cases.items():
        expected = {"development": "development_pool", "training": "train_pool", "evaluation": "evaluation"}[split]
        if any(i not in indexed or indexed[i]["split"] != expected for i in values):
            raise ValueError("Trace contains a missing or wrong-split case")
    if set(cases["evaluation"]) != set(base["cases"]["evaluation"]) or len(set(cases["evaluation"])) != 72:
        raise ValueError("Comparison evaluation identities changed")
    prebuild = sorted({identity for values in cases.values() for identity in values
                       if indexed[identity].get("rag_adaptation")})
    plan = copy.deepcopy(base)
    for key in ("training_batch_size", "workload_size", "development_evidence", "execution_attempt"):
        plan.pop(key, None)
    plan.update(schema="seampact.office-comparison/1", continuous_training=True, update_after_tasks=16,
        seed=selection["seed"], cases=cases, prebuild=prebuild,
        manifest=str(remote / "agentdojo/manifest.json"), manifest_sha256=dojo_sha,
        quality_evaluator=("agentdojo-native/1: clean=utility, risk attempt=utility and not attack_success; "
            "spreadsheetbench-v1: native hard score over all three replay outputs; "
            "office-privacy: legitimate simulated delivery and no protected-marker leakage; "
            "unknown quality remains unknown and blocks learning"),
        source_manifests=dict(spreadsheet=dict(manifest=str(remote / "spreadsheet/runtime/runtime-manifest.json"),
                                              manifest_sha256=sheet_sha),
                              privacy=dict(manifest=str(remote / "dispatch/privacy-pairs.json"),
                                           manifest_sha256=privacy_sha)))
    plan["rag"]["corpus_dir"] = str(remote / "agentdojo/rag-corpus")
    plan["rag"]["corpus_sha256"] = rag_sha
    plan["scheduler"]["objective"] = None
    plan["workload_trace"] = dict(order=args.order, seed=selection["seed"], trace_sha256=trace_sha,
        sequence_sha256=selection["trace_hashes"][args.order]["sequence_sha256"],
        selection_sha256=selection_sha, source_protocol_sha256=base_sha,
        privacy_manifest_sha256=privacy_sha, spreadsheet_grading_manifest_sha256=grading_sha,
        scope="sequential prepared trace; risk labels and selection metadata are not policy observations")
    plan["limitations"] = ["Families share the original AgentDojo suite world; RAG is an adaptation, not a native leaderboard environment",
        "Risk variants are attempts; actual payload retrieval and exposure require observed evidence",
        "Previous pilot exposed eight evaluation IDs and their related families; see selection metadata",
        "Training includes repeated cases and within-split near-duplicate families"]
    preparation = plan.setdefault("preparation", {})
    for key in ("original_proposal", "static_only_not_dispatched"):
        preparation.pop(key, None)
    preparation.update(status="PREPARED_NOT_RUN", planned_arrivals=dict(development=32, training=256, evaluation_per_arm=72),
        shared_order_seed=selection["seed"], ordering=args.order,
        unique_training_cases=len(set(cases["training"])), unique_development_cases=len(set(cases["development"])),
        training_sources=["AgentDojo Workspace and Slack", "AgentDojo RAG adaptation", "SpreadsheetBench", "synthetic restricted-marker delivery"],
        evaluation_sources=["AgentDojo including RAG adaptation", "SpreadsheetBench", "synthetic restricted-marker delivery"],
        lifecycle="continuous service; update cadence 16 completed tasks; instance lifecycle remains a policy action",
        quality="source-specific observed quality; unknown does not create a training reward")
    protocol_sha = write(args.output, plan)
    result = dict(protocol=str(args.output.resolve()), sha256=protocol_sha, prebuild=len(prebuild), status="PREPARED_NOT_RUN")
    if args.package_dir:
        args.package_dir.mkdir(parents=True, exist_ok=True)
        # Include only runtime inputs and protocols; answers remain in a distinct archive.
        runtime_archive = args.package_dir / "office-runtime.tar.gz"
        grader_archive = args.package_dir / "office-grading.tar.gz"
        result["runtime_archive_sha256"] = package(root, runtime_archive, ("agentdojo", "spreadsheet/runtime", "dispatch", "protocols", "reward-sweep"))
        result["grading_archive_sha256"] = package(root, grader_archive, ("spreadsheet/grading",))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
