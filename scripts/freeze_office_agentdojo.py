"""Create an office AgentDojo source inventory, not a selected evaluation set.

Run with the pinned AgentDojo environment. Store the output outside model and
policy observations. The fixed-template attacks use only upstream ground-truth
tool execution to find injection positions; no model is invoked.
"""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess

import agentdojo
from agentdojo.attacks import load_attack
from agentdojo.task_suite.load_suites import get_suite


REVISION = "089ed468cf3ed0322acc66b0211f26d9d90dbf60"
VERSION = "v1.2.2"
ATTACKS = ("direct", "ignore_previous", "system_message", "injecagent")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, help="new private output directory")
    args = parser.parse_args()
    source = Path(agentdojo.__file__).resolve().parents[2]
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(source), *arguments], text=True).strip()
    if importlib.metadata.version("agentdojo") != "0.1.35" or git("rev-parse", "HEAD") != REVISION:
        raise ValueError("AgentDojo import does not match the pinned source and package version")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("AgentDojo checkout is not clean; do not freeze ambiguous source")
    args.output.mkdir(parents=True, exist_ok=False)
    archive = args.output / "agentdojo-task-source.zip"
    # Upstream's runs/ tree holds tens of thousands of unrelated old run logs.
    # Preserve every source/data file, dependency declaration and license.
    scope = ["src", "pyproject.toml", "uv.lock", "LICENSE"]
    subprocess.run(["git", "-C", str(source), "archive", "--format=zip", "-o", str(archive.resolve()), REVISION, *scope], check=True)
    data = dict(schema="seampact.agentdojo.office-inventory.v1", benchmark_version=VERSION,
                source=dict(repository="https://github.com/ethz-spylab/agentdojo", revision=REVISION,
                            package_version="0.1.35", archive=archive.name, archive_scope=scope,
                            sha256=hashlib.sha256(archive.read_bytes()).hexdigest()),
                note="Inventory only; no formal subset, RAG mapping, split or attack selection is implied.",
                suites={})
    for suite_name in ("workspace", "slack"):
        suite = get_suite(VERSION, suite_name)
        attacks = {name: load_attack(name, suite, None) for name in ATTACKS}
        tasks = {}
        for task_id, task in suite.user_tasks.items():
            entry = dict(prompt=task.PROMPT, attacks={})
            for name, attack in attacks.items():
                try:
                    # Resolve once; preserve an explicit reason for tasks with no reachable vector.
                    attack.get_injection_candidates(task)
                    entry["attacks"][name] = {
                        injection_id: attack.attack(task, injection)
                        for injection_id, injection in suite.injection_tasks.items()}
                except Exception as error:
                    entry["attacks"][name] = dict(error_type=type(error).__name__, error=str(error))
            tasks[task_id] = entry
        data["suites"][suite_name] = dict(
            tasks=tasks,
            injection_goals={key: task.GOAL for key, task in suite.injection_tasks.items()},
            tool_names=[tool.name for tool in suite.tools])
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    (args.output / "inventory.json").write_bytes(content.encode("utf-8"))
    (args.output / "inventory.sha256").write_text(
        hashlib.sha256(content.encode("utf-8")).hexdigest() + "  inventory.json\n", encoding="ascii")
    print("Saved source and task/attack inventory:", args.output)


if __name__ == "__main__":
    main()
