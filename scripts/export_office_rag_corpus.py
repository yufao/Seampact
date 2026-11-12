"""Export the full AgentDojo drive corpus and original attack variants.

These are text inside the upstream simulated drive, not binary DOCX/XLSX
files. Attack variants are preparation artifacts until the runtime can prove
that retrieval actually reads their contaminated contents.
"""
import argparse
import hashlib
import json
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suite


REFERENCES = {"user_task_25": "25", "user_task_28": "0", "user_task_30": "7",
              "user_task_31": "7", "user_task_32": "7", "user_task_33": "19",
              "user_task_36": "7", "user_task_37": "7",
              "user_task_29": "3", "user_task_34": "11"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    checksum = args.manifest.with_suffix(args.manifest.suffix + ".sha256").read_text(encoding="ascii").split()[0]
    if sha(raw) != checksum:
        raise ValueError("Subset manifest checksum mismatch")
    manifest = json.loads(raw)
    cases = [c for c in manifest["cases"] if c.get("rag_adaptation")]
    used = {c["user_task_id"] for c in cases}
    if not used <= set(REFERENCES) or not set(REFERENCES) - {"user_task_29", "user_task_34"} <= used:
        raise ValueError("Selected references do not match the frozen RAG task mapping")
    suite = get_suite("v1.2.2", "workspace")
    clean = suite.load_and_inject_default_environment({})
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "clean").mkdir()
    catalog = {}
    for file_id in sorted(clean.cloud_drive.files, key=int):
        file = clean.cloud_drive.files[file_id]
        content = file.content.encode("utf-8")
        path = Path("clean") / f"drive-{file_id}.txt"
        (args.output / path).write_bytes(content)
        catalog[file_id] = dict(filename=file.filename, owner=file.owner, shared_with=file.shared_with,
                                path=path.as_posix(), sha256=sha(content))
    variants = {}
    for case in cases:
        if not case.get("injections"):
            continue
        task_id, attack_id = case["user_task_id"], case["injection_task_id"]
        original = suite.load_and_inject_default_environment(case["injections"])
        changed = [fid for fid, file in original.cloud_drive.files.items()
                   if file.content != clean.cloud_drive.files[fid].content]
        if not changed:
            raise ValueError("Frozen RAG injection does not modify a drive document")
        folder = Path("attacked") / f"{task_id}-{attack_id}"
        (args.output / folder).mkdir(parents=True)
        files = {}
        for file_id in changed:
            data = original.cloud_drive.files[file_id].content.encode("utf-8")
            path = folder / f"drive-{file_id}.txt"
            (args.output / path).write_bytes(data)
            files[file_id] = dict(path=path.as_posix(), sha256=sha(data))
        variants[case["id"]] = dict(original_changed_file_ids=changed,
                                    method="native_injected_source", files=files,
                                    readiness="pending_verified_retrieval_path")
    metadata = dict(schema="seampact.office-rag-corpus.v1", source_manifest_sha256=checksum,
                    source_revision=manifest["source"]["revision"],
                    note="Simulated drive text, not binary office files; writable targets stay in native environment.",
                    references={key: REFERENCES[key] for key in sorted(used)}, files=catalog, attacked_variants=variants)
    data = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (args.output / "corpus.json").write_bytes(data)
    (args.output / "corpus.json.sha256").write_text(sha(data) + "  corpus.json\n", encoding="ascii")
    print("Exported", len(catalog), "drive documents and", len(variants), "pending attack variants")


if __name__ == "__main__":
    main()
