"""Bound full QACD check and VPC3 calibration on the current answer version."""

import copy

from seampact.vpc3_scene import VPC3Scene


class VisualScene(VPC3Scene):
    def __init__(self, config, context):
        super().__init__(dict(config, required_units=[u for u in config.get("required_units", [])
                                                     if u == config["unit_id"]]), context)
        self.config = copy.deepcopy(config)
        self.qacd_id = config.get("qacd_unit_id")
        if len(config.get("required_units", [])) != len(set(config.get("required_units", []))) or \
                set(config.get("required_units", [])) - {self.unit_id, self.qacd_id}:
            raise ValueError("visual obligations must name distinct actually configured Units")
        threshold = config.get("simple_risk_threshold")
        if threshold is not None and (type(threshold) not in (int, float) or not 0 <= threshold <= 1):
            raise ValueError("simple_risk_threshold must be a frozen value in [0,1]")
        self.budget = config["max_unit_calls"]
        if type(self.budget) is not int or self.budget < 1:
            raise ValueError("visual scene requires a finite max_unit_calls budget")

    def reset(self, task, seed):
        super().reset(task, seed)
        self.current = self.request["original_answer"]
        self.version, self.used, self.checks, self.calibrated = 0, 0, {}, False
        self.outputs, self.last_calibration = [], None
        return self._decision()

    def _decision(self):
        check = self.checks.get(self.version)
        candidates = [dict(id="finish", units=[], legal=True, features=[])]
        if self.used < self.budget:
            if not self.calibrated:
                candidates.append(dict(id="calibrate", units=[self.unit_id], legal=True, features=[]))
            if self.qacd_id and check is None:
                candidates.append(dict(id="check", units=[self.qacd_id], legal=True, features=[]))
        # Obligations apply to remaining required work, not to every later event.
        completed = {v["unit_id"] for v in self.outputs}
        pending = [u for u in self.config.get("required_units", []) if u not in completed]
        required = set(pending[:1])  # discharge the task's required sequence under FIFO
        if pending:
            for c in candidates:
                c["legal"] = required <= set(c["units"])
        legal_actions = [c for c in candidates if required <= set(c["units"])]
        if not legal_actions:
            raise ValueError("visual budget/candidates cannot discharge required work")
        all_action = next((c["id"] for c in legal_actions if c["id"] == "check"), None)
        all_action = all_action or next((c["id"] for c in legal_actions if c["id"] == "calibrate"), legal_actions[0]["id"])
        simple = self.config.get("simple_risk_threshold")
        simple_action = ("check" if check is None and any(c["id"] == "check" for c in legal_actions) else
                         "calibrate" if check is not None and simple is not None and check >= simple
                         and any(c["id"] == "calibrate" for c in legal_actions) else "finish")
        if required:
            simple_action = legal_actions[0]["id"]
        return dict(kind="decision", event="visual_answer_returned", required_units=list(required),
            observation=[], candidates=candidates,
            scene_observation=dict(modalities=["text", "image"], scopes=["visual_qa"],
                available_inputs=["question", "answer", "image"], question_chars=len(self.request["question"]),
                answer_chars=len(self.current), answer_version=self.version, evidence_count=len(self.outputs),
                prior_calls=self.used, remaining_budget=self.budget-self.used, risk=check),
            references=dict(required_only=legal_actions[0]["id"], all_applicable=all_action,
                            simple=simple_action if simple is not None else None))

    def step(self, candidate):
        action = candidate["id"]
        if action == "finish":
            # Reuse the original native correctness evaluator for the actual final answer.
            original = self.request["original_answer"]
            before, before_soft = self.correctness(original, self.reference)
            after, after_soft = self.correctness(self.current, self.reference)
            metrics = dict(native_quality=int(after), native_before_quality=int(before),
                native_correction_success=int(after and not before), native_harm=int(before and not after),
                native_quality_gain=int(after)-int(before),
                native_changed=int(self.current.casefold() != original.casefold()))
            if self.reference["category"] == "ocr":
                metrics.update(native_ocr_soft_quality=after_soft, native_ocr_before_soft_quality=before_soft)
            return dict(kind="terminal", terminated=True, truncated=False, response=self.current,
                program_metrics=metrics, evaluation_metrics=copy.deepcopy(metrics),
                evidence=dict(image=copy.deepcopy(self.request["image"]), question=self.request["question"],
                    original_answer=original, final_answer=self.current, module_outputs=copy.deepcopy(self.outputs)),
                reason="Actual final visual answer delivered")
        if action == "calibrate":
            request = dict(self.request, original_answer=self.current)
            result = self.context.invoke(self.unit_id, {self.argument: request})
            if result.get("request_id") != request["request_id"] or result.get("original_answer") != self.current:
                raise ValueError("calibration returned an unmatched current answer")
            answer = result.get("calibrated_answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("calibration did not return an actual answer")
            changed = answer.strip().casefold() != self.current.casefold()
            if result.get("changed") is not changed:
                raise ValueError("calibration changed flag disagrees with its answer")
            self.current = answer.strip()
            self.version += int(changed)
            self.calibrated = True
            uid = self.unit_id
        elif action == "check":
            result = self.context.invoke(self.qacd_id, dict(question=self.request["question"],
                answer=self.current, image=self.request["image"]["value"]))
            risk = result.get("risk_score")
            if type(risk) not in (int, float) or not 0 <= risk <= 1:
                raise ValueError("QACD did not return an actual risk_score in [0,1]")
            self.checks[self.version] = risk
            uid = self.qacd_id
        else:
            raise ValueError("unknown visual action")
        self.used += 1
        self.outputs.append(dict(unit_id=uid, answer_version=self.version, result=copy.deepcopy(result)))
        return self._decision()


def make_scene(config, context):
    return VisualScene(config, context)
