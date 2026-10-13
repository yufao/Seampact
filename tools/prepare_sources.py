"""Register source scope, not source-specific partition answers.

Consume the existing public-source preflight without importing native libraries.
"""
import argparse
import hashlib
import json
from pathlib import Path

LABELS = {"mmct-image":"MMCT ImageAgent", "crag-text":"LlamaIndex CRAG",
          "abc-policy":"NeMo ABC Bot", "disp-offline":"DiSP"}
ENTRIES = {
    "mmct-image":"ImageAgent.setup → ImageAgent.run / run_stream; native SDK lifecycle to be validated",
    "crag-text":"corrective_rag_pack.ipynb workflow definition and top-level workflow.run invocation",
    "abc-policy":"examples/bots/abc config.yml + prompts/rails/kb; native LLMRails entry must be added to scope",
    "disp-offline":"scripts/self_purification.sh → self_purify/self_purification.py; separate finetune/evaluation scripts"}


def prepare(preflight, destination, portable=False, extension=None):
    preflight, destination = Path(preflight).resolve(), Path(destination).resolve()
    evidence = json.loads(preflight.read_text(encoding="utf-8"))
    supplement=json.loads(Path(extension).read_text(encoding="utf-8")) if extension else None
    sources = {}
    for source in evidence["sources"]:
        identity = source["target_id"]
        root = preflight.parent / source["checkout"]
        paths = []
        records=list(source["files"])
        if supplement and identity==supplement["target_id"]:
            if source["revision"]!=supplement["revision"]:
                raise ValueError("source extension revision mismatch")
            for record in supplement["files"]:
                if record["status"]!="FROZEN":
                    raise ValueError("unfrozen extension artifact")
                previous=next((r for r in records if r["path"]==record["path"]),None)
                if previous and previous["sha256"]!=record["sha256"]:
                    raise ValueError("extension disagrees with original immutable preflight")
                if not previous:records.append(record)
        for record in records:
            path = root / record["path"]
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("source preflight mismatch: " + identity + "/" + record["path"])
            paths.append(record["path"])
            if portable:
                target = destination.parent / "sources" / identity / record["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        sources[identity] = dict(name=LABELS.get(identity,identity),
            root="sources/"+identity if portable else str(root), files=paths,
            entry=ENTRIES[identity],
            revision=source["revision"], scope="selected public artifacts; whole dependency closure UNKNOWN")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding="utf-8")
    return sources


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("preflight")
    parser.add_argument("destination")
    parser.add_argument("--portable",action="store_true")
    parser.add_argument("--extension",help="explicit same-revision source extension; original preflight remains unchanged")
    args=parser.parse_args()
    result=prepare(args.preflight,args.destination,args.portable,args.extension)
    print(json.dumps({"sources":len(result),"files":sum(len(s["files"]) for s in result.values())}))
