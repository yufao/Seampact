"""Add two held-out drive-document tasks to distinct train/dev RAG families.

This only prepares a new source manifest; it does not execute any tasks.
"""
import argparse
import hashlib
import json
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    if sha(raw) != args.manifest.with_suffix(args.manifest.suffix + ".sha256").read_text(encoding="ascii").split()[0]:
        raise ValueError("Original AgentDojo manifest identity changed")
    manifest = json.loads(raw)
    if manifest.get("schema") != "seampact.office-agentdojo-subset.v1":
        raise ValueError("Unexpected AgentDojo subset")
    used = {case["id"] for case in manifest["cases"]}
    inventory_raw = args.inventory.read_bytes()
    expected = args.inventory.with_name("inventory.sha256").read_text(encoding="ascii").split()[0]
    if sha(inventory_raw) != expected or expected != manifest["source"]["inventory_sha256"]:
        raise ValueError("AgentDojo source inventory identity changed")
    inventory = json.loads(inventory_raw)
    templates = ("direct", "ignore_previous", "system_message", "injecagent")
    # Use only existing train-family prompts and inventory-provided payloads.
    # The four upstream targets were selected before observing outcomes.
    for original in list(manifest["cases"]):
        if original["split"] != "train_pool" or original.get("injection_task_id"):
            continue
        suite, task = original["suite"], original["user_task_id"]
        task_number = int(task.removeprefix("user_task_"))
        for index, template in enumerate(templates):
            attack = f"injection_task_{(task_number + index) % 4}"
            options = inventory["suites"][suite]["tasks"][task]["attacks"][template]
            injections = options.get(attack)
            if not isinstance(injections, dict) or not injections:
                continue
            identity = f"train_pool:{suite}:{task}:{template}:{attack}"
            if identity not in used:
                manifest["cases"].append(dict(original, id=identity, injection_task_id=attack,
                    injections=injections, attack_template=template))
                used.add(identity)
    held = manifest["selection"]["held_out_unselected"]["workspace"]
    for number, split in ((29, "train_pool"), (34, "development_pool")):
        task = f"user_task_{number}"
        family = f"workspace:family-{number}"
        if task not in held or any(case["family_id"] == family for case in manifest["cases"]):
            raise ValueError("Selected task is not an independent held family")
        identity = f"{split}:workspace:{task}:rag:clean"
        if identity in used:
            raise ValueError("Duplicate RAG case")
        manifest["cases"].append(dict(id=identity, split=split, suite="workspace", user_task_id=task,
            family_id=family, rag_adaptation=True, readiness="pending_verified_retrieval_path"))
        template, attack = ("direct", "injection_task_0") if number == 29 else ("ignore_previous", "injection_task_2")
        injections = inventory["suites"]["workspace"]["tasks"][task]["attacks"][template].get(attack)
        if not isinstance(injections, dict) or not injections:
            raise ValueError("Selected RAG source injection is unavailable")
        manifest["cases"].append(dict(id=f"{split}:workspace:{task}:rag:{template}:{attack}",
            split=split, suite="workspace", user_task_id=task, family_id=family, rag_adaptation=True,
            readiness="pending_verified_retrieval_path", injection_task_id=attack,
            injections=injections, attack_template=template))
        held.remove(task)
    manifest["selection"]["training_rag_extension"] = dict(
        source_manifest_sha256=sha(raw), tasks=["user_task_29", "user_task_34"],
        note="Held-out families allocated separately to training and development; retrieval and native scoring not yet exercised.")
    manifest["selection"]["training_attack_extension"] = dict(inventory_sha256=expected,
        method="four fixed upstream templates and targets 0-3 on existing train tasks when inventory provides injection positions; target rotates by task number",
        note="Variants share original tasks and families; no attack is executed during preparation.")
    content = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(content)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(sha(content) + "  " + args.output.name + "\n", encoding="ascii")
    print(json.dumps(dict(manifest_sha256=sha(content), added_training=1, added_development=1,
                          status="PREPARED_NOT_RUN")))


if __name__ == "__main__":
    main()
