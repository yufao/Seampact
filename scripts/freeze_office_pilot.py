"""Freeze the agreed small pilot from the existing private v3 task manifest."""
import argparse
import hashlib
import json
from pathlib import Path

DEVELOPMENT = ["development_pool:workspace:user_task_2:clean", "development_pool:workspace:user_task_3:clean",
               "development_pool:slack:user_task_8:clean", "development_pool:slack:user_task_18:direct:injection_task_3"]
TRAINING = ["train_pool:workspace:user_task_5:clean", "train_pool:workspace:user_task_9:clean",
            "train_pool:workspace:user_task_11:clean", "train_pool:workspace:user_task_13:direct:injection_task_2",
            "train_pool:slack:user_task_7:direct:injection_task_1", "train_pool:slack:user_task_9:clean",
            "train_pool:slack:user_task_12:clean", "train_pool:slack:user_task_13:clean"]
EVALUATION = ["evaluation:workspace:user_task_25:clean", "evaluation:workspace:user_task_28:clean",
              "evaluation:workspace:user_task_0:clean", "evaluation:workspace:user_task_6:clean",
              "evaluation:slack:user_task_0:clean", "evaluation:slack:user_task_4:clean",
              "evaluation:workspace:user_task_25:direct:injection_task_3",
              "evaluation:slack:user_task_6:system_message:injection_task_4"]
PREBUILD = ["evaluation:workspace:user_task_25:clean",
            "evaluation:workspace:user_task_25:direct:injection_task_3"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("settings", type=Path, help="private project ID, RAG unit and scheduler settings")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest, corpus = args.manifest.resolve(), args.corpus.resolve()
    manifest_hash = sha(manifest)
    if manifest.with_suffix(manifest.suffix + ".sha256").read_text(encoding="ascii").split()[0] != manifest_hash:
        raise ValueError("Manifest checksum mismatch")
    corpus_hash = sha(corpus / "corpus.json")
    if (corpus / "corpus.json.sha256").read_text(encoding="ascii").split()[0] != corpus_hash:
        raise ValueError("Corpus checksum mismatch")
    source = json.loads(manifest.read_text(encoding="utf-8"))
    if json.loads((corpus / "corpus.json").read_text(encoding="utf-8"))["source_manifest_sha256"] != manifest_hash:
        raise ValueError("Corpus and manifest do not match")
    ids = {item["id"] for item in source["cases"]}
    if set(DEVELOPMENT + TRAINING + EVALUATION + PREBUILD) - ids:
        raise ValueError("The frozen source manifest lacks a selected case")
    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    plan = dict(schema="seampact.office-pilot/1", manifest=str(manifest), manifest_sha256=manifest_hash,
        project_id=settings["project_id"], rag=dict(corpus_dir=str(corpus), corpus_sha256=corpus_hash,
                                                     capability_id=settings["rag_unit_id"]),
        scheduler=settings["scheduler"], policies=settings["policies"],
        resource_source=settings["resource_source"], seed=20261111,
        learner_config=settings.get("learner_config", {}), deadline_s=source["protocol"]["timeout_s"],
        cases=dict(development=DEVELOPMENT, training=TRAINING, evaluation=EVALUATION), prebuild=PREBUILD,
        limitations=["Small descriptive pilot, not evidence of a statistical advantage",
                     "Training includes within-split near-duplicate families: workspace 5/11 and Slack 7/12",
                     "RAG adaptation is not the native leaderboard task environment"])
    if "guard_policy" in settings:
        plan["guard_policy"] = settings["guard_policy"]
    if "bindings" in settings:
        plan["bindings"] = settings["bindings"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(dict(protocol=str(args.output.resolve()), sha256=sha(args.output))))


if __name__ == "__main__":
    main()
