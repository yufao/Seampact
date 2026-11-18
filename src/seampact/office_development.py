"""Admission of frozen office development tasks through the project host."""
import hashlib
import json
from pathlib import Path

from seampact.agent_runtime import model_turn_limit
from seampact.office_application import build_hooks
from seampact.office_privacy import SimulatedOfficeTask


DEVELOPMENT_SOURCES = frozenset({"office_ci", "office_summedits", "office_routing", "office_access", "office_links"})


def grading_by_case(source, location, runtime):
    raw = Path(location["grading_manifest"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != location["grading_sha256"]:
        raise ValueError("办公开发题评分清单内容已变化")
    grading = json.loads(raw)
    prefix = "seampact." + source.replace("_", "-")
    if (runtime.get("schema") != prefix + "-runtime/1" or grading.get("schema") != prefix + "-grading/1"
            or runtime.get("grading_sha256", location["grading_sha256"]) != location["grading_sha256"]):
        raise ValueError("办公开发题清单格式无效")
    cases = runtime["cases"]
    references = {ref["id"]: ref for ref in grading["cases"]}
    if (not cases or len(cases) != len(references) or len(references) != len(grading["cases"])
            or {case["id"] for case in cases} != references.keys()
            or any(case.get("split") != "development_pool"
                   or case["family_id"] != references[case["id"]]["family_id"] for case in cases)):
        raise ValueError("办公开发题与评分清单未按原题及家族对齐")
    return references


def start_development_task(platform, body, *, internal_task_id, pilot_config):
    case, project_id = body["case"], body["project_id"]
    reference = case["_grading"]
    options = dict(max_model_turns=model_turn_limit(pilot_config.get("max_model_turns")),
                   allowed_capabilities=[], allowed_workspace_tools=["list_files", "read_file"])
    source = case["_source"]
    if source in {"office_summedits", "office_links"}:
        from seampact.office_links import score_office_links
        from seampact.office_summedits import score_summedits
        scorer = score_summedits if source == "office_summedits" else score_office_links
        options["evaluator"] = lambda task: scorer(reference, task)
    elif source in DEVELOPMENT_SOURCES:
        from seampact.office_access import score_access_receipts
        from seampact.office_ci import score_ci_receipts
        from seampact.office_routing import score_routing_receipts
        scorer = {"office_ci": score_ci_receipts, "office_routing": score_routing_receipts,
                  "office_access": score_access_receipts}[source]
        environment = SimulatedOfficeTask(platform, case, project_id, internal_task_id,
                                         receipt_scorer=lambda receipts: scorer(reference, receipts))
        options.update(external_tools=environment.tool_specs(), external_execute=environment.execute,
                       evaluator=environment.evaluate)
    else:
        raise ValueError("未知办公开发题来源")
    options.update(build_hooks(platform, internal_task_id, pilot_config))
    inputs = case["agent_input"]
    files = {name: value.encode("utf-8") for name, value in inputs["documents"].items()}
    prompt = inputs["request"] + "\nTask files in the data area: " + ", ".join(files)
    with platform.deployments.lock, platform.projects.lock:
        return platform.chat.start(
            dict(project_id=project_id, messages=[dict(role="user", content=prompt)], deadline_s=pilot_config["deadline_s"],
                 **({"execution_timeout_s": pilot_config["execution_timeout_s"]} if "execution_timeout_s" in pilot_config else {})),
            runtime_options=options, expected_model=pilot_config.get("business_model"),
            input_files=files, internal_task_id=internal_task_id,
            capability_snapshot=platform.capabilities.public(), apply_project_application=False,
            metadata=dict(arm=pilot_config["mode"], case=case["id"], family_id=case["family_id"],
                          source=case["_source"], fixture_sha256=case["_manifest"]["manifest_sha256"]))
