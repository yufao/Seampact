"""Small parity check against native AgentDojo ground-truth execution."""

import json
import pytest

pytest.importorskip("agentdojo")

from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
from agentdojo.attacks import load_attack
from agentdojo.task_suite.load_suites import get_suite
from seampact.office_benchmark import AgentDojoOfficeTask


@pytest.mark.parametrize("suite_name", ["workspace", "slack"])
@pytest.mark.parametrize("attacked", [False, True])
def test_native_task_and_attack_scoring(suite_name, attacked):
    suite = get_suite("v1.2.2", suite_name)
    task = next(iter(suite.user_tasks.values()))
    attack_task = next(iter(suite.injection_tasks.values())) if attacked else None
    injections = load_attack("direct", suite, None).attack(task, attack_task) if attacked else {}
    native_utility, native_attack = suite.run_task_with_pipeline(
        GroundTruthPipeline(task), task, attack_task, injections)
    bridge = AgentDojoOfficeTask(suite_name, task.ID,
        injection_task_id=attack_task.ID if attack_task else None, injections=injections)
    assert bridge.prompt == task.PROMPT
    assert {item["function"]["name"] for item in bridge.tool_specs()} == {f.name for f in suite.tools}
    calls = task.ground_truth(bridge._env.model_copy(deep=True))
    for call in calls:
        result = bridge.execute(call.function, dict(call.args), call.id)
        assert result["error"] is None
    scores = bridge.evaluate(task.GROUND_TRUTH_OUTPUT)
    assert scores == {"utility": native_utility, "attack_success": native_attack if attacked else None}
    with pytest.raises(RuntimeError):
        bridge.execute("unused", {})


def test_blocked_calls_remain_in_native_trace():
    suite = get_suite("v1.2.2", "slack")
    user = suite.get_user_task_by_id("user_task_0")
    attack = suite.get_injection_task_by_id("injection_task_5")
    injections = load_attack("direct", suite, None).attack(user, attack)
    bridge = AgentDojoOfficeTask("slack", user.ID, injection_task_id=attack.ID, injections=injections)
    calls = attack.ground_truth(bridge._env.model_copy(deep=True))
    bridge.record_message({"role": "assistant", "tool_calls": [
        {"id": str(i), "function": {"name": call.function, "arguments": json.dumps(call.args)}}
        for i, call in enumerate(calls)]})
    # The upstream security criterion tests requested calls rather than state.
    # They were all blocked before execute, but must still appear in the trace.
    assert bridge.evaluate("")["attack_success"] is True


def test_rag_tracks_original_attack_and_rejects_stale_retrieval():
    suite = get_suite("v1.2.2", "workspace")
    user = suite.get_user_task_by_id("user_task_28")
    attack = suite.get_injection_task_by_id("injection_task_10")
    injections = load_attack("direct", suite, None).attack(user, attack)
    bridge = AgentDojoOfficeTask("workspace", user.ID, injection_task_id=attack.ID,
                                injections=injections, rag=True)
    offered = {item["function"]["name"] for item in bridge.tool_specs()}
    assert not offered.intersection({"search_files", "search_files_by_filename", "get_file_by_id", "list_files"})
    assert "list_drive_file_metadata" in offered
    metadata = json.loads(bridge.execute("list_drive_file_metadata", {})["content"])
    assert all("content" not in item for item in metadata)
    name = "office-reference/drive-0.txt"
    payload = next(iter(injections.values()))
    text = bridge._env.cloud_drive.files["0"].content
    bridge.configure_rag_reference(name, {name: "digest"}, [name], [payload])
    hit = {"output": {"index_identity": "index", "results": [
        {"name": name, "digest": "digest", "content": text}]}}
    bridge.record_retrieval_receipt(hit)
    bridge.record_retrieval_observation(json.dumps({"archive_ref": "ref", "result": hit}))
    assert bridge.rag_status()["payload_retrieved"]
    assert bridge.rag_status()["payload_visible"]
    assert bridge.rag_status()["path_valid"]
    assert bridge.execute("create_file", {"filename": "new.txt", "content": "new"})["error"] is None
    bridge.record_retrieval_receipt(hit)
    assert bridge.rag_status()["stale_retrieval"]
    assert not bridge.rag_status()["path_valid"]
    bridge.record_retrieval_observation(json.dumps({"preview": "incomplete", "truncated": True}))
    assert bridge.rag_status()["observation_incomplete"]
