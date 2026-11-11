"""TaskSpec events before exposure, after drafting and before publication.

Call plans contain installed Unit IDs and finite input/value-port mappings.
They create no source objects, fine boundaries, hidden predictions or models.
"""

import json

from seampact.scenes.evaluation_evidence import passive_value
from seampact.scenes.call_artifacts import CallArtifacts
from seampact.scenes.material_review_scene import BUSINESS_PROMPT, REPORT_TOOL


STAGES = ("intake", "draft_review", "publication")
EVENTS = dict(intake="untrusted_material_received", draft_review="business_draft_returned",
              publication="before_publication")
TASK_FIELDS = ("evidence", "material", "question", "request", "image", "images")
MODEL_INPUT_FIELDS = {"request", "question", "material", "evidence"}


def _label_key(value):
    """Frozen label-map keys: strings, decimal integers, JSON booleans only."""
    if type(value) is str:
        return value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    raise ValueError("label mapping requires an actual plain string/integer/boolean scalar")


class MultiSourceReviewScene:
    def __init__(self, config, context):
        self.config, self.context = passive_value(config), context
        if self.config.get("schema") != "review-events/1":
            raise ValueError("multi-source review requires review-events/1")
        self.draft_source = self.config.get("draft_source", "business_model")
        if self.draft_source not in {"business_model", "provided_material"}:
            raise ValueError("draft_source must select the business model or provided material")
        self.checks = self.config.get("checks", [])
        if not isinstance(self.checks, list):
            raise ValueError("checks must be a frozen list of Unit call plans")
        self.model_preparation_budget = self.config.get("model_preparation_budget", 0)
        if type(self.model_preparation_budget) is not int or self.model_preparation_budget < 0:
            raise ValueError("model_preparation_budget must be a finite nonnegative call count")
        preparation_calls = 0
        identities = set()
        for check in self.checks:
            if (not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"]
                    or check["id"] in identities or check.get("stage") not in STAGES
                    or not isinstance(check.get("calls"), list) or not check["calls"]):
                raise ValueError("check identity, event stage and complete call plan are required")
            if check["id"] in {"continue", "publish", "abstain"}:
                raise ValueError("check identity conflicts with a business continuation")
            if (type(check.get("requires", [])) is not list
                    or any(field not in TASK_FIELDS for field in check.get("requires", []))):
                raise ValueError("check applicability may use only visible input fields")
            if type(check.get("required", False)) is not bool:
                raise ValueError("required must be a boolean task obligation")
            if "business_continuation" in check and (type(check["business_continuation"]) is not bool
                    or check["business_continuation"] and
                    (check["stage"] == "intake" or self.draft_source != "business_model")):
                raise ValueError("business continuation requires a post-draft check and the business model")
            preparations = check.get("model_preparations", [])
            if type(preparations) is not list:
                raise ValueError("model preparations must be a frozen list")
            generated_ids = set()
            for preparation in preparations:
                if (type(preparation) is not dict
                        or set(preparation) != {"id", "count", "system_prompt", "fields"}
                        or type(preparation["id"]) is not str or not preparation["id"]
                        or preparation["id"] in generated_ids
                        or type(preparation["count"]) is not int or preparation["count"] <= 0
                        or type(preparation["system_prompt"]) is not str or not preparation["system_prompt"].strip()
                        or type(preparation["fields"]) is not list or not preparation["fields"]
                        or any(type(field) is not str or field not in MODEL_INPUT_FIELDS for field in preparation["fields"])
                        or len(set(preparation["fields"])) != len(preparation["fields"])):
                    raise ValueError("model preparation requires a distinct id, fixed positive count, prompt and visible input fields")
                generated_ids.add(preparation["id"])
                preparation_calls += preparation["count"]
            requirements = check.get("result_requirements", [])
            if type(requirements) is not list:
                raise ValueError("result requirements must be a frozen list")
            for requirement in requirements:
                if type(requirement) is not dict or set(requirement) not in ({"value", "equals"}, {"value", "nonempty_string"}):
                    raise ValueError("result requirement needs an exact equality or nonempty string")
                selector = requirement["value"]
                if (type(selector) is not dict or set(selector) != {"$result"} or type(selector["$result"]) is not list
                        or len(selector["$result"]) < 2 or selector["$result"][0] != check["id"]
                        or type(selector["$result"][1]) is not int or not 0 <= selector["$result"][1] < len(check["calls"])):
                    raise ValueError("result requirements may select only this check's actual results")
                if "nonempty_string" in requirement and requirement["nonempty_string"] is not True:
                    raise ValueError("nonempty_string must be true")
                if "equals" in requirement and type(requirement["equals"]) not in (str, int, bool, float, type(None)):
                    raise ValueError("result equality needs a plain scalar")
            identities.add(check["id"])
            for call in check["calls"]:
                if (not isinstance(call, dict) or not isinstance(call.get("unit_id"), str)
                        or not call["unit_id"] or not isinstance(call.get("arguments"), dict)):
                    raise ValueError("call plans require installed Unit IDs and argument mappings")
                CallArtifacts(call.get("output_artifacts", {}))
            if set(check.get("updates", {})) - {"material", "answer", "evidence"}:
                raise ValueError("a returned value may only update material, evidence or the current answer")
            evidence = check.get("updates", {}).get("evidence")
            if evidence is not None and (type(evidence) is not list or not evidence or any(
                    type(item) is not dict or set(item) != {"$result"} or type(item["$result"]) is not list
                    or len(item["$result"]) < 2 or item["$result"][0] != check["id"]
                    or type(item["$result"][1]) is not int or not 0 <= item["$result"][1] < len(check["calls"])
                    for item in evidence)):
                raise ValueError("evidence updates require this check's actual result selectors")
            if "input_version" in check and (type(check["input_version"]) is not str or not check["input_version"]):
                raise ValueError("an explicit check input version must be a nonempty scope identity")
            additional = check.get("additional_observations", [])
            if type(additional) is not list:
                raise ValueError("additional observations must be a frozen list")
            for observation in [check.get("observation", {}), *additional]:
                if (type(observation) is not dict or type(observation.get("source_call", len(check["calls"]) - 1)) is not int
                        or not 0 <= observation.get("source_call", len(check["calls"]) - 1) < len(check["calls"])):
                    raise ValueError("an observation must name an actual call in this check")
                mini = observation.get("kind") == "minicheck_binary"
                labels = observation.get("label_map", {})
                if mini and "label_map" in observation and (type(labels) is not dict or not labels):
                    raise ValueError("MiniCheck label_map requires explicit original codes and binary meanings")
                for source, target in labels.items():
                    if type(source) is not str or target not in ({"supported", "unsupported"} if mini else {"benign", "injection", "unknown"}):
                        raise ValueError("original label meanings must be explicitly frozen for this observation kind")
            self._validate_bindings({key: value for key, value in check.items() if key != "calls"},
                                    check["id"], generated_ids)
            for call in check["calls"]:
                self._validate_bindings({key: value for key, value in call.items() if key != "arguments"},
                                        check["id"], generated_ids)
                self._validate_bindings(call["arguments"], check["id"], generated_ids, allow_ports=True)
        if preparation_calls > self.model_preparation_budget:
            raise ValueError("declared model preparations exceed the frozen model_preparation_budget")

    def _validate_bindings(self, value, check_id=None, generated_ids=(), *, allow_ports=False):
        if type(value) is dict:
            if "$task" in value:
                path = value["$task"]
                if not isinstance(path, list) or not path or path[0] not in TASK_FIELDS:
                    raise ValueError("call mappings cannot read hidden task fields")
            if "$generated" in value:
                path = value["$generated"]
                if (set(value) != {"$generated"} or type(path) is not list or len(path) < 2
                        or any(type(part) not in (str, int) for part in path)
                        or path[0] != check_id or path[1] not in generated_ids):
                    raise ValueError("generated ports must select this check's declared model preparation")
            if "$port" in value:
                path = value["$port"]
                if (not allow_ports or set(value) != {"$port"} or type(path) is not list or len(path) < 2
                        or type(path[0]) is not str or not path[0]
                        or type(path[1]) is not int or path[1] < 0
                        or any(type(part) not in (str, int) for part in path)):
                    raise ValueError("original result ports are available only to explicit call arguments")
            if "$map_join" in value:
                if set(value) != {"$map_join"}:
                    raise ValueError("a value selector cannot have additional fields")
                self._map_join_spec(value["$map_join"])
            for item in value.values():
                self._validate_bindings(item, check_id, generated_ids, allow_ports=allow_ports)
        elif type(value) is list:
            for item in value:
                self._validate_bindings(item, check_id, generated_ids, allow_ports=allow_ports)

    @staticmethod
    def _map_join_spec(spec):
        required = {"value", "item_path", "joiner"}
        if (type(spec) is not dict or not required <= set(spec) or set(spec) - required - {"item_length"}
                or type(spec["value"]) is not dict or set(spec["value"]) != {"$result"}
                or type(spec["value"]["$result"]) is not list or len(spec["value"]["$result"]) < 2
                or type(spec["item_path"]) is not list
                or any(type(part) not in (str, int) for part in spec["item_path"])
                or type(spec["joiner"]) is not str
                or "item_length" in spec and (type(spec["item_length"]) is not int or spec["item_length"] <= 0)):
            raise ValueError("map_join requires actual returned values, a finite item path and literal joiner")
        return spec

    def _path(self, value, path):
        if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
            raise ValueError("value selectors require an explicit string/integer path")
        for part in path:
            value = value[part]
        return value

    def _resolve(self, template):
        if type(template) is dict:
            operators = set(template) & {"$task", "$report", "$result", "$port", "$map_join",
                                         "$generated", "$version", "$length", "$output"}
            if operators:
                if len(template) != 1:
                    raise ValueError("a value selector cannot have additional fields")
                operator = next(iter(operators))
                path = template[operator]
                if operator == "$output":
                    if self.call_artifacts is None:
                        raise ValueError("$output is available only in its selected original call")
                    return self.call_artifacts.output(path)
                if operator == "$task":
                    return self._path(self.visible, path)
                if operator == "$report":
                    if self.report is None:
                        raise ValueError("a business report is not yet available")
                    return self._path(self.report, path)
                if operator == "$result":
                    # Only actually completed calls are inserted into results.
                    return self._path(self.results, path)
                if operator == "$port":
                    return self._path(self.result_ports, path)
                if operator == "$map_join":
                    spec = self._map_join_spec(path)
                    values = self._resolve(spec["value"])
                    if type(values) is not list:
                        raise ValueError("map_join requires an actual returned list")
                    joined = []
                    for item in values:
                        leaf = self._path(item, spec["item_path"])
                        if (type(leaf) is not list or any(type(part) is not str for part in leaf)
                                or "item_length" in spec and len(leaf) != spec["item_length"]):
                            raise ValueError("map_join leaves must be original finite string lists of the declared length")
                        joined.append(spec["joiner"].join(leaf))
                    return joined
                if operator == "$generated":
                    return self._path(self.generated, path)
                if operator == "$version":
                    if path is not True:
                        raise ValueError("$version must explicitly select the current report version")
                    return self.version
                value = self._resolve(path)
                if type(value) not in (str, list, dict):
                    raise ValueError("$length requires an actual finite returned collection")
                return len(value)
            return {key: self._resolve(value) for key, value in template.items()}
        if type(template) is list:
            return [self._resolve(value) for value in template]
        return template

    def reset(self, task, seed):
        self.task = task
        self.visible = passive_value({key: task[key] for key in TASK_FIELDS if key in task})
        if self.draft_source == "provided_material" and (type(self.visible.get("material")) is not str
                or not self.visible["material"].strip()):
            raise ValueError("a provided publication draft requires actual nonempty material")
        def image_metadata(value):
            if type(value) is not dict:
                raise ValueError("image metadata must be an explicit object")
            if any(key in value and (type(value[key]) is not int or value[key] <= 0)
                   for key in ("width", "height")):
                raise ValueError("image dimensions must be actual positive integer metadata")
            return {key: value[key] for key in ("input", "input_sha256", "width", "height") if key in value}
        if "image" in self.visible:
            self.visible["image"] = image_metadata(self.visible["image"])
        if "images" in self.visible:
            if type(self.visible["images"]) is not list:
                raise ValueError("images must be a list of actual image metadata")
            self.visible["images"] = [image_metadata(value) for value in self.visible["images"]]
        self.report, self.version, self.stage = None, 0, "intake"
        self.material_version = 1
        self.evidence_version = 1 if "evidence" in self.visible else 0
        self.evidence_updates = []
        self.records, self.results, self.result_ports, self.used = [], {}, {}, set()
        self.generated, self.preparation_records, self.preparation_calls = {}, [], 0
        self.raw_records = []
        self.call_artifacts = None
        self.abstained = False
        # No business-model call occurs before this decision is returned.
        return self._decision()

    def _model(self):
        # Freeze the common-material prefix order for every policy and process;
        # cache use remains whatever the actual provider reports.
        visible = passive_value({field: self.visible[field] for field in TASK_FIELDS if field in self.visible})
        if "image" in visible:
            # Paths, names, hashes and dataset fields are not visual evidence.
            visible["image"] = {key: visible["image"][key] for key in ("width", "height")
                                if key in visible["image"]}
        if "images" in visible:
            visible["images"] = [{key: image[key] for key in ("width", "height") if key in image}
                                 for image in visible["images"]]
        visible.update(prior_check_observations=passive_value(self.records))
        if self.report is not None:
            visible["current_review"] = self.report
        before = len(self.context.model_calls)
        response = self.context.complete([dict(role="system", content=BUSINESS_PROMPT),
            dict(role="user", content=json.dumps(visible, ensure_ascii=False))], [REPORT_TOOL])
        choices = response.get("choices") or []
        if len(choices) != 1 or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
            raise ValueError("business model did not complete one report")
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if len(calls) == 1 and calls[0].get("function", {}).get("name") == "submit_material_review":
            report = json.loads(calls[0]["function"]["arguments"])
        elif not calls and choices[0]["finish_reason"] == "stop":
            report = json.loads(message.get("content", ""))
        else:
            raise ValueError("business model returned a foreign or incomplete tool request")
        properties = REPORT_TOOL["function"]["parameters"]["properties"]
        if (type(report) is not dict or set(report) != set(properties)
                or type(report["answer"]) is not str or not report["answer"].strip()
                or any(report[key] not in properties[key]["enum"] for key in ("text_assessment", "image_assessment"))):
            raise ValueError("business model returned an invalid report")
        self.report, self.version = report, self.version + 1
        self.records.append(dict(kind="business_assessment", status="available", source="chat",
            event="business_review_returned:" + str(self.version), input_version="answer:" + str(self.version),
            cost_refs=["model_call:" + str(index) for index in range(before, len(self.context.model_calls))],
            value=dict(text=report["text_assessment"], image=report["image_assessment"])))

    def _available(self):
        return [check for check in self.checks if check["stage"] == self.stage and check["id"] not in self.used
                and all(self.visible.get(field) is not None for field in check.get("requires", []))
                and all(self.visible.get(field) is not None for preparation in check.get("model_preparations", [])
                        for field in preparation["fields"])]

    def _prepare_models(self, check):
        """Selected, independently prompted business samples, through the normal ledger."""
        self.generated[check["id"]] = {}
        for preparation in check.get("model_preparations", []):
            inputs = passive_value({field: self.visible[field] for field in preparation["fields"]})
            messages = [dict(role="system", content=preparation["system_prompt"]),
                        dict(role="user", content=json.dumps(inputs, ensure_ascii=False))]
            outputs = self.generated[check["id"]][preparation["id"]] = []
            record = dict(check_id=check["id"], id=preparation["id"], source="chat",
                          fields=list(preparation["fields"]), requested=preparation["count"],
                          completed=0, status="RUNNING", cost_refs=[])
            self.preparation_records.append(record)
            for _ in range(preparation["count"]):
                if self.preparation_calls >= self.model_preparation_budget:
                    record["status"] = "FAILED"
                    self._remember_failure_evidence()
                    raise ValueError("selected model preparation exceeds its frozen call budget")
                before = len(self.context.model_calls)
                self.preparation_calls += 1
                try:
                    # No current draft, prior checks, teacher input or shared
                    # conversation is included. Each request starts afresh.
                    response = self.context.complete(passive_value(messages), None)
                    choices = response.get("choices") or []
                    message = (choices[0].get("message") or {}) if len(choices) == 1 else {}
                    content = message.get("content")
                    if (len(choices) != 1 or choices[0].get("finish_reason") != "stop"
                            or message.get("tool_calls") or type(content) is not str or not content.strip()):
                        raise ValueError("business preparation did not return one complete text sample")
                    outputs.append(content)
                except Exception:
                    record["status"] = "FAILED"
                    raise
                finally:
                    record["completed"] = len(outputs)
                    record["cost_refs"].extend("model_call:" + str(index)
                                               for index in range(before, len(self.context.model_calls)))
                    self._remember_failure_evidence()
            record["status"] = "COMPLETED"
            self._remember_failure_evidence()

    def _pending_required(self):
        return [check for check in self.checks if check["stage"] == self.stage
                and check.get("required", False) and check["id"] not in self.used]

    def _outstanding_required(self):
        return [check for check in self.checks if check.get("required", False) and check["id"] not in self.used]

    def _failure_evidence(self):
        return dict(request=self.task.get("request"), original_material=self.task.get("material"),
            current_evidence=passive_value(self.visible.get("evidence", [])),
            evidence_updates=passive_value(self.evidence_updates), observations=passive_value(self.records),
            original_results=self.results, generated_inputs=passive_value(self.generated),
            model_preparations=passive_value(self.preparation_records))

    def _remember_failure_evidence(self):
        artifacts = [artifact for calls in self.results.values() for result in calls
            if type(result) is dict and type(result.get("artifacts", {})) is dict
            for artifact in result.get("artifacts", {}).values()]
        self.context.record_failure_evidence(self._failure_evidence(), output_artifacts=artifacts)

    def preview_calls(self, candidate):
        """Resolve only values already available; never prepare output artifacts."""
        check = next((item for item in self._available() if item["id"] == candidate["id"]), None)
        if check is None:
            return []
        previews = []
        for call in check["calls"]:
            try:
                arguments = self._resolve(call["arguments"])
            except (KeyError, IndexError, TypeError, ValueError):
                arguments = None
            previews.append(dict(unit_id=call["unit_id"], arguments=arguments))
        return previews

    def _input_version(self, check):
        if "input_version" in check:
            return check["input_version"]
        if (check.get("observation") or {}).get("kind") == "image_score":
            if len(self.visible.get("images", [])) > 1 or ("image" in self.visible and self.visible.get("images")):
                raise ValueError("multiple image checks require an explicit per-image input_version")
            return "image:1"
        return "material:" + str(self.material_version) if check["stage"] == "intake" else "answer:" + str(self.version)

    def _simple_choice(self, checks, required_choice, continuation):
        """Visible-input routing with at most one optional text verifier."""
        if required_choice is not None:
            return required_choice
        kind = lambda check: (check.get("observation") or {}).get("kind")
        if self.stage == "intake" and type(self.visible.get("material")) is str and self.visible["material"].strip():
            for check in checks:
                if "material" in check.get("updates", {}):
                    return check["id"]
        if (self.stage == "draft_review" and self.report is not None
                and self.report["text_assessment"] in {"uncertain", "contradicted"}):
            text_kinds = {"nli", "minicheck_binary", "selfcheck_score"}
            # Do not add a second verifier after a completed evidence check,
            # even if the paid business continuation remains uncertain.
            checked = any(check["stage"] == self.stage and check["id"] in self.used
                          and kind(check) in text_kinds for check in self.checks)
            if not checked:
                evidence_checks = [check for check in checks if kind(check) in {"nli", "minicheck_binary"}
                                   and self.visible.get("evidence")]
                if evidence_checks:
                    return evidence_checks[0]["id"]
                for check in checks:
                    if kind(check) == "selfcheck_score":
                        return check["id"]
        if self.stage == "publication" and ("image" in self.visible or self.visible.get("images")):
            for check in checks:
                if kind(check) == "image_score":
                    return check["id"]
        return continuation

    def _decision(self):
        checks = self._available()
        images = ([self.visible["image"]] if "image" in self.visible else []) + self.visible.get("images", [])
        dimensions_known = all(type(image.get(key)) is int and image[key] > 0
                               for image in images for key in ("width", "height"))
        candidates = [dict(id=check["id"], action_type="invoke", legal=True, features=[],
            units=list(dict.fromkeys(call["unit_id"] for call in check["calls"])),
            input_version=self._input_version(check),
            coverage_text=self.report["answer"] if self.report else "") for check in checks]
        final = self.stage == "publication"
        pending = self._pending_required()
        if not pending and (not final or not self._outstanding_required()):
            candidates.append(dict(id="publish" if final else "continue", action_type="finish" if final else "continue",
                                   units=[], legal=True, features=[]))
        if self.config.get("abstain_answer") and not final and not self._outstanding_required():
            candidates.append(dict(id="abstain", action_type="finish", units=[], legal=True, features=[]))
        if not candidates:
            raise ValueError("required check is not applicable to available task inputs")
        kinds = {(check.get("observation") or {}).get("kind") for check in self.checks
                 if all(self.visible.get(field) is not None for field in check.get("requires", []))}
        observation = dict(modalities=["text"] + (["image"] if images else []),
            scopes=["general_text"], available_inputs=["question"] + (["answer"] if self.report else []) +
                (["tool_result"] if self.stage == "intake" and "material" in self.visible else []) +
                (["paper_evidence"] if "evidence" in self.visible else []) + (["image"] if images else []),
            question_chars=len(self.visible.get("question", self.visible.get("request", ""))),
            material_chars=len(self.visible["material"]) if type(self.visible.get("material")) is str else None,
            image_count=len(images), image_pixels=sum(image["width"] * image["height"] for image in images) if dimensions_known else None,
            answer_chars=len(self.report["answer"]) if self.report else 0,
            evidence_count=len(self.visible.get("evidence", [])), answer_version=self.version,
            material_version=self.material_version,
            prior_calls=len(self.context.calls), remaining_budget=len(checks), risk=None,
            observation_records=passive_value(self.records),
            result_status={kind: "missing" if kind in kinds else "not_applicable"
                           for kind in ("nli", "image_score", "prompt_injection", "privacy", "minicheck_binary", "selfcheck_score")})
        first = checks[0]["id"] if checks else candidates[-1]["id"]
        required_choice = next((check["id"] for check in pending if check in checks), None)
        if pending and required_choice is None:
            raise ValueError("required check is not applicable to available task inputs")
        return dict(kind="decision", event=EVENTS[self.stage],
            control_event=EVENTS[self.stage], required_units=[], candidates=candidates,
            scene_observation=observation, references=dict(required_only=required_choice or ("publish" if final else "continue"),
                all_applicable=first, simple=self._simple_choice(checks, required_choice, "publish" if final else "continue")))

    def step(self, candidate):
        identity = candidate["id"]
        if ((identity == "continue" and self._pending_required()) or
                (identity in {"publish", "abstain"} and self._outstanding_required())):
            raise ValueError("required check has not completed")
        if identity == "abstain" and self.config.get("abstain_answer"):
            self.abstained = True
            self.report = dict(answer=self.config["abstain_answer"], text_assessment="uncertain", image_assessment="uncertain")
            return self._terminal()
        if identity == "publish" and self.stage == "publication":
            return self._terminal()
        if identity == "continue" and self.stage != "publication":
            if self.stage == "intake":
                if self.draft_source == "provided_material":
                    self.report = dict(answer=self.visible["material"], text_assessment="uncertain",
                                       image_assessment="not_applicable")
                    self.version += 1
                else:
                    self._model()
                self.stage = "draft_review"
            else:
                self.stage = "publication"
            return self._decision()
        check = next((check for check in self._available() if check["id"] == identity), None)
        if check is None:
            raise ValueError("check is unavailable at the current business event")
        self.results[identity] = []
        self.result_ports[identity] = []
        input_version = self._input_version(check)
        self._prepare_models(check)
        before = len(self.context.calls)
        for call in check["calls"]:
            artifacts = CallArtifacts(call.get("output_artifacts", {}))
            contract = self.context.catalog.unit(call["unit_id"]).get("completion_contract") if artifacts.declarations else None
            artifacts.prepare(call["arguments"], contract)
            self.call_artifacts = artifacts
            try:
                result = self.context.invoke(call["unit_id"], self._resolve(call["arguments"]))
            finally:
                self.call_artifacts = None
            # Keep the actual return before conversion or artifact capture can
            # fail. The context owns the passive-value boundary and records a
            # blocked snapshot when a live value cannot safely be detached.
            self.results[identity].append(result)
            self._remember_failure_evidence()
            snapshot = passive_value(result)
            # The checked data port remains distinct from its business and
            # terminal projection. A snapshot dictionary cannot restore a
            # RecordSnapshot or other typed original result on the next call.
            self.result_ports[identity].append(result)
            captured = artifacts.capture(snapshot)
            if captured:
                snapshot["artifacts"] = captured
            self.results[identity][-1] = snapshot
            self._remember_failure_evidence()
        for requirement in check.get("result_requirements", []):
            try:
                actual = self._resolve(requirement["value"])
                if "equals" in requirement:
                    expected = requirement["equals"]
                    ok = type(actual) is type(expected) and actual == expected
                else:
                    ok = type(actual) is str and bool(actual.strip())
            except (KeyError, IndexError, TypeError, ValueError):
                ok = False
            if not ok:
                self._remember_failure_evidence()
                raise ValueError("actual returned result did not satisfy the frozen requirement")
        for observation in [check.get("observation", {}), *check.get("additional_observations", [])]:
            source_call = observation.get("source_call", len(check["calls"]) - 1)
            value = self._resolve(observation.get("value", {"$result": [identity, source_call]}))
            kind = observation.get("kind", "other")
            raw = None
            if kind == "prompt_injection":
                value = dict(value)
                value["label"] = observation.get("label_map", {}).get(value.get("raw_label"), "unknown")
            elif kind == "minicheck_binary":
                raw = passive_value(value)
                if "label_map" in observation:
                    key = _label_key(value)
                    if key not in observation["label_map"]:
                        raise ValueError("original MiniCheck code is absent from the frozen label_map")
                    value = observation["label_map"][key]
                elif type(value) is not str or value not in {"supported", "unsupported"}:
                    raise ValueError("MiniCheck observation requires an original binary string or an explicit label_map")
            record = dict(kind=kind, status="available", source=check["calls"][source_call]["unit_id"],
                event=EVENTS[self.stage] + ":check_returned:" + str(len(self.records)),
                input_version=input_version,
                cost_refs=["unit_call:" + str(index) for index in range(before, len(self.context.calls))], value=value)
            if kind == "minicheck_binary":
                # Unknown record kinds feed the existing "other" online feature.
                # Keep raw codes in detached terminal evidence, so only the frozen
                # binary meaning enters the unchanged scheduler representation.
                self.raw_records.append(dict(record, kind="raw:minicheck_binary", value=raw))
            self.records.append(record)
        for field, binding in check.get("updates", {}).items():
            value = self._resolve(binding)
            if field == "evidence":
                if type(value) is not list or not value or any(type(item) is not str or not item.strip() for item in value):
                    raise ValueError("evidence updates require actual nonempty returned text")
                before_version = self.evidence_version
                self.visible["evidence"] = passive_value(value)
                self.evidence_version += 1
                self.evidence_updates.append(dict(check_id=identity, input_version=input_version,
                    previous_evidence_version=before_version, evidence_version=self.evidence_version,
                    sources=[dict(result_path=passive_value(item["$result"]),
                        cost_ref="unit_call:" + str(before + item["$result"][1])) for item in binding]))
            elif type(value) is not str:
                raise ValueError("material/answer updates require an original returned string")
            elif field == "answer":
                if self.report is None:
                    raise ValueError("no business answer exists to update")
                self.report["answer"] = value
                self.version += 1
            elif field == "material":
                self.visible["material"] = value
                self.material_version += 1
        self.used.add(identity)
        self._remember_failure_evidence()
        if (check.get("business_continuation", self.stage == "draft_review")
                and self.draft_source == "business_model"):
            # A paid evidence check can change the answer only through the
            # actual business continuation, not through a synthetic reward or
            # a fabricated corrected answer. Publication resumes only when
            # explicitly requested, so exact publication updates stay intact.
            self._model()
        return self._decision()

    def _terminal(self):
        return dict(kind="terminal", terminated=True, truncated=False, response=passive_value(self.report),
            output_artifacts=[passive_value(artifact) for calls in self.results.values() for result in calls
                if type(result) is dict for artifact in result.get("artifacts", {}).values()],
            program_metrics=dict(delivered=1, abstained=int(self.abstained)),
            evidence=dict(request=self.task["request"], original_material=self.task.get("material"),
                reference_evidence=self.task.get("evidence", []), final_review=passive_value(self.report),
                current_evidence=passive_value(self.visible.get("evidence", [])), evidence_updates=passive_value(self.evidence_updates),
                observations=passive_value(self.records), raw_observations=passive_value(self.raw_records),
                original_results=passive_value(self.results), generated_inputs=passive_value(self.generated),
                model_preparations=passive_value(self.preparation_records),
                evaluation_reference=self.task.get("evaluation_reference", {})))


def make_scene(config, context):
    return MultiSourceReviewScene(config, context)
