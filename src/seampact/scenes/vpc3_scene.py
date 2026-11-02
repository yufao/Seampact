"""Online VPC3 calibration after a real, already generated visual answer.

This bridge calls the bound, unchanged ``calibrate(request)`` Unit. It neither
generates expert evidence nor substitutes another model for the native expert.
Cached ``expert_evidence`` is deliberately unsupported: a fast cache lookup
must not be reported as the online cost of running a visual expert.

Factory config requires source_root, unit_id, skip_features and
calibrate_features. Optional image_root resolves relative image paths;
request_argument defaults to ``request``. The source_root's
scripts/audit_native_five.py and woodpecker_qwen/native_router{,_v2}.py must be
listed in the enclosing scene adapter dependencies so a run freezes them.

Tasks contain id, request={request_id, image, question, original_answer}, and
reference={category, references}. Images are real local paths, either strings
or native {source: path, value: ...} objects. Reference answers are available
only to the terminal native evaluator. They never enter a decision event,
expert request, or teacher evidence. Optional upstream_costs records the
caller's already incurred answer-generation costs; this bridge does not count
them as current model calls or latency.

Observation schema: question_chars, original_answer_chars, answer_type_boolean,
answer_type_integer, answer_type_color, answer_type_entity, answer_type_open_text.
Unknown native answer types produce five zero indicators; the true route is
retained in terminal evidence. Candidate vectors are explicitly configured,
normally module_count, processes_result, uses_model. A single decision is a
contextual decision problem; the common RL learners can execute it unchanged.
"""

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ANSWER_TYPES = ("boolean", "integer", "color", "entity", "open_text")
REFERENCE_CATEGORIES = {"object_presence", "color", "spatial_relation", "count", "ocr"}
REQUEST_FIELDS = {"request_id", "image", "question", "original_answer"}


def _native_functions(source_root):
    """Load the original pure route and scorer without importing model weights."""
    paths = [source_root / "scripts/audit_native_five.py",
             source_root / "woodpecker_qwen/native_router.py",
             source_root / "woodpecker_qwen/native_router_v2.py"]
    content = [path.read_bytes() for path in paths]
    identity = hashlib.sha256(str(source_root).encode() + b"".join(content)).hexdigest()
    name = "seampact_vpc3_native_" + identity
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(source_root / "woodpecker_qwen")]
        sys.modules[name] = package
        # Load the relative dependency first. Compiling the actual source
        # avoids writing bytecode into the original Harness checkout.
        for suffix, path, data in (("native_router", paths[1], content[1]),
                                   ("native_router_v2", paths[2], content[2]),
                                   ("audit_native_five", paths[0], content[0])):
            qualified = name + "." + suffix
            spec = importlib.util.spec_from_file_location(qualified, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified] = module
            exec(compile(data, str(path), "exec"), module.__dict__)
    return (sys.modules[name + ".native_router_v2"].route_question,
            sys.modules[name + ".audit_native_five"].correctness)


class VPC3Scene:
    def __init__(self, config, context):
        self.config, self.context = copy.deepcopy(config), context
        self.source_root = Path(config["source_root"]).resolve()
        self.route_question, self.correctness = _native_functions(self.source_root)
        self.unit_id = config["unit_id"]
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("VPC3 scene requires a bound calibration Unit ID")
        self.argument = config.get("request_argument", "request")
        if not isinstance(self.argument, str) or not self.argument:
            raise ValueError("VPC3 request_argument must name the native request parameter")
        for key in ("skip_features", "calibrate_features"):
            self.config.setdefault(key, [])  # common representation is formed by SceneSession
        self.required = config.get("required_units", [])
        if not isinstance(self.required, list) or len(self.required) != len(set(self.required)) or \
                set(self.required) - {self.unit_id}:
            raise ValueError("VPC3 required_units may contain only its configured calibration Unit")
        self.image_root = Path(config["image_root"]).resolve() if config.get("image_root") else None
        self.pending = False

    def reset(self, task, seed):
        # There is no policy-side resampling or seed-based fabrication here.
        request = copy.deepcopy(task["request"])
        if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
            raise ValueError("VPC3 online request requires only request_id, image, question and original_answer; "
                             "cached expert_evidence and evaluation labels are not accepted")
        for key in ("request_id", "question", "original_answer"):
            if not isinstance(request[key], str) or not request[key].strip():
                raise ValueError("VPC3 request requires a nonempty string: " + key)
            request[key] = request[key].strip()
        image = request["image"]
        if isinstance(image, str):
            image_path = Path(image).expanduser()
        elif isinstance(image, dict) and image.get("source") in {"path", "file"} and \
                isinstance(image.get("value"), str):
            image_path = Path(image["value"]).expanduser()
        else:
            raise ValueError("VPC3 scene requires a real local image path; URLs and inline base64 are unsupported")
        if not image_path.is_absolute():
            if self.image_root is None:
                raise ValueError("Relative VPC3 image paths require image_root")
            image_path = self.image_root / image_path
        image_path = image_path.resolve()
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        request["image"] = dict(source="path", value=str(image_path))
        reference = copy.deepcopy(task["reference"])
        if not isinstance(reference, dict) or reference.get("category") not in REFERENCE_CATEGORIES:
            raise ValueError("VPC3 reference needs an original five-category correctness category")
        refs = reference.get("references")
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
            raise ValueError("VPC3 terminal correctness requires real, nonempty reference answers")
        self.request, self.reference = request, reference
        self.upstream_costs = copy.deepcopy(task.get("upstream_costs", {}))
        if not isinstance(self.upstream_costs, dict):
            raise ValueError("VPC3 upstream_costs must be a caller-supplied object")
        self.route = self.route_question(request["question"])
        answer_type = self.route["answer_type"]
        self.pending = True
        return dict(kind="decision", event="visual_answer_returned",
            scene_observation=dict(modalities=["text", "image"], scopes=["visual_qa"],
                available_inputs=["question", "answer", "image"], question_chars=len(request["question"]),
                answer_chars=len(request["original_answer"]), evidence_count=0, answer_version=0,
                prior_calls=0, remaining_budget=1, risk=None),
            observation=[len(request["question"]), len(request["original_answer"]),
                         *(int(answer_type == kind) for kind in ANSWER_TYPES)],
            candidates=[dict(id="skip", features=copy.deepcopy(self.config["skip_features"]),
                             units=[], legal=True),
                        dict(id="calibrate:" + self.unit_id,
                             features=copy.deepcopy(self.config["calibrate_features"]),
                             units=[self.unit_id], legal=True)],
            required_units=list(self.required),
            references=dict(required_only="calibrate:" + self.unit_id if self.required else "skip",
                            all_applicable="calibrate:" + self.unit_id,
                            simple=self._simple_action()))

    def _simple_action(self):
        # Only a task-owner supplied rule on the original cheap route is used.
        kinds = self.config.get("simple_answer_types")
        if kinds is None:
            return None
        if not isinstance(kinds, list) or any(k not in ANSWER_TYPES for k in kinds):
            raise ValueError("simple_answer_types requires actual native route types")
        return "calibrate:" + self.unit_id if self.required or self.route["answer_type"] in kinds else "skip"

    def step(self, candidate):
        if not self.pending:
            raise ValueError("VPC3 is not at a pending visual-answer decision")
        self.pending = False
        action = candidate["id"]
        result = None
        if action == "skip" and not candidate.get("units"):
            final = self.request["original_answer"]
        elif action == "calibrate:" + self.unit_id and candidate.get("units") == [self.unit_id]:
            result = self.context.invoke(self.unit_id, {self.argument: copy.deepcopy(self.request)})
            if not isinstance(result, dict) or result.get("request_id") != self.request["request_id"] or \
                    result.get("original_answer") != self.request["original_answer"]:
                raise ValueError("VPC3 returned an unmatched native request or original answer")
            final = result.get("calibrated_answer")
            if not isinstance(final, str) or not final.strip():
                raise ValueError("VPC3 did not return a nonempty native calibrated answer")
            final = final.strip()
            changed = final.casefold() != self.request["original_answer"].casefold()
            if type(result.get("changed")) is not bool or result["changed"] != changed:
                raise ValueError("VPC3 changed flag disagrees with its native final answer")
        else:
            raise ValueError("VPC3 received an unknown calibration action")
        # Native references are opened for scoring only after the chosen action
        # finishes. Their category is never used to construct the observation.
        before, before_soft = self.correctness(self.request["original_answer"], self.reference)
        after, after_soft = self.correctness(final, self.reference)
        metrics = dict(native_quality=int(after), native_before_quality=int(before),
            native_correction_success=int(after and not before), native_harm=int(before and not after),
            native_changed=int(final.casefold() != self.request["original_answer"].casefold()),
            native_quality_gain=int(after) - int(before))
        if self.reference["category"] == "ocr":
            metrics.update(native_ocr_soft_quality=after_soft, native_ocr_before_soft_quality=before_soft)
        evidence = dict(request_id=self.request["request_id"], image=copy.deepcopy(self.request["image"]),
            question=self.request["question"], original_answer=self.request["original_answer"],
            final_answer=final, native_route=copy.deepcopy(self.route),
            selected_action=action, calibration_result=copy.deepcopy(result),
            cost_scope=dict(expert_evidence_mode="online", original_answer_already_generated=True,
                            upstream_costs_source="caller_supplied_not_measured_by_this_scene",
                            upstream_costs=self.upstream_costs))
        return dict(kind="terminal", terminated=True, truncated=False, evidence=evidence,
                    program_metrics=metrics, evaluation_metrics=copy.deepcopy(metrics), response=final,
                    reason="Original VPC3 calibration completed" if result is not None else "Original visual answer retained")


def make_scene(config, context):
    return VPC3Scene(config, context)
