"""Collect frozen native office cases through the running ordinary Pi host.

This is the host reference arm, not the eight-library scheduling comparison.
Each admitted task is saved, including failures and unknown native scores.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import time
from urllib.request import Request, urlopen
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8880")
    parser.add_argument("--split", default="evaluation")
    parser.add_argument("--case-id", action="append")
    args = parser.parse_args()
    content = args.manifest.read_bytes()
    manifest = json.loads(content)
    fingerprint = hashlib.sha256(content).hexdigest()
    cases = [c for c in manifest["cases"] if c["split"] == args.split and
             c.get("readiness") == "native_ready" and
             (not args.case_id or c["id"] in args.case_id)]
    if not cases:
        raise ValueError("No frozen ready cases selected")
    random.Random(manifest["selection"]["seed"]).shuffle(cases)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "manifest.json").write_bytes(content)
    root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(args.output / "runtime-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, directories, files in os.walk(root / "src" / "seampact"):
            directories[:] = [d for d in directories if d not in {"node_modules", "__pycache__"}]
            for name in files:
                path = Path(folder) / name
                archive.write(path, path.relative_to(root))
        archive.write(Path(__file__).resolve(), "scripts/run_office_native.py")
    def api(path, data=None):
        request = Request(args.url + path,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=90) as response:
            return json.load(response)
    def save(name, value):
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = api("/api/state")
    save("frozen-runtime.json", dict(models=state["models"], scheduler=state["scheduler"]["execution"]["config"],
        capability_packages=state["capability_packages"], code_execution=state["code_execution"],
        cases=[c["id"] for c in cases], manifest_sha256=fingerprint,
        source_archive_sha256=hashlib.sha256((args.output / "runtime-source.zip").read_bytes()).hexdigest(),
        arm="pi_native_reference", comparison_scope="native host reference only"))
    records = []
    for case in cases:
        file_id = hashlib.sha256(case["id"].encode("utf-8")).hexdigest()[:20]
        admission = api("/api/experiments/office/native", dict(
            manifest=str(args.manifest.resolve()), manifest_sha256=fingerprint,
            case_id=case["id"], project_id=args.project_id, host_reference=True))
        save("admitted-" + file_id + ".json", admission)
        print(json.dumps(dict(case=case["id"], task_id=admission["task_id"], status="RUNNING")), flush=True)
        while True:
            task = api("/api/tasks/" + admission["task_id"])
            if task["status"] not in {"RUNNING", "CANCELLING"} and task.get("workspace_status"):
                break
            time.sleep(2)
        save(file_id + ".json", task)
        record = dict(case=case["id"], task_id=task["id"], status=task["status"],
                      evaluation=task.get("evaluation"), evaluation_error=task.get("evaluation_error"),
                      latency_ms=task.get("latency_ms"), model_calls=task.get("model_calls"),
                      host=task.get("host"), error=task.get("error"))
        records.append(record)
        save("results.json", dict(planned=len(cases), completed=len(records), records=records))
        print(json.dumps(record, ensure_ascii=False), flush=True)
    print("Native reference collection finished; no eight-library comparison is implied.", flush=True)


if __name__ == "__main__":
    main()
