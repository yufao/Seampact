"""PaperGraph whole or explicitly bound fine entries, preserving full obligations.

Fine entries must exist in the catalog. This bridge does not certify EXPORT or
turn the original mandatory loop into optional work. Partial coverage requires
an explicit new task policy; unchecked claims remain unchecked in evidence.
"""

import copy


class PaperGraphScene:
    def __init__(self, config, context):
        self.config, self.context = config, context
        self.mode = config["granularity"]
        if self.mode not in {"whole", "fine_all", "fine_optional"}:
            raise ValueError("select whole, fine_all or an explicit fine_optional TaskSpec")
        if self.mode == "fine_optional" and not config.get("partial_policy_basis"):
            raise ValueError("partial claim verification requires an explicit new task policy basis")

    def reset(self, task, seed):
        self.turn = copy.deepcopy(task["turn"])
        self.results, self.claims, self.checked, self.verified_indices = [], None, set(), set()
        self.stage = "entry"
        self.required = bool(self.config.get("required", False))
        uid = self.config["whole_unit_id"] if self.mode == "whole" else self.config["extract_unit_id"]
        return dict(kind="decision", event="paper_answer_returned", observation=[],
            scene_observation=self._facts(), required_units=[uid] if self.required else [],
            candidates=[dict(id="skip", units=[], legal=not self.required, features=[]),
                        dict(id="run", units=[uid], legal=True, features=[])],
            references=dict(required_only="run" if self.required else "skip", all_applicable="run", simple=None))

    def _facts(self, text=None):
        return dict(modalities=["text"], scopes=["paper_qa"],
            available_inputs=["question", "answer", "paper_evidence"] + (["claim"] if text else []),
            question_chars=len(self.turn.get("question_text", "")), answer_chars=len(text or self.turn.get("model_answer", "")),
            evidence_count=len(self.results), answer_version=0, prior_calls=len(self.context.calls),
            remaining_budget=(len(self.claims) - len(self.checked)) if self.claims is not None else None, risk=None)

    def _next(self):
        remaining = [i for i in range(len(self.claims)) if i not in self.checked]
        if not remaining:
            return self._terminal()
        index = remaining[0]
        claim = self.claims[index]
        uid = self.config["verify_unit_id"]
        optional = self.mode == "fine_optional"
        # Original full loop order remains fixed. Optional policy can skip the
        # current claim; it cannot reorder original mandatory occurrences.
        return dict(kind="decision", event="paper_answer_returned", observation=[],
            scene_observation=self._facts(claim["claim_text"]), required_units=[] if optional else [uid],
            candidates=[dict(id="omit:" + str(index), units=[], legal=optional, features=[], coverage_text=claim["claim_text"]),
                        dict(id="verify:" + str(index), units=[uid], legal=True, features=[], coverage_text=claim["claim_text"])],
            references=dict(required_only="omit:" + str(index) if optional else "verify:" + str(index),
                            all_applicable="verify:" + str(index), simple=None))

    def step(self, candidate):
        if self.stage == "entry":
            if candidate["id"] == "skip":
                return self._terminal()
            if self.mode == "whole":
                self.results = self.context.invoke(self.config["whole_unit_id"], {"turn": self.turn})
                return self._terminal()
            self.claims = self.context.invoke(self.config["extract_unit_id"], {"turn": self.turn})
            if not isinstance(self.claims, list) or any(not isinstance(c.get("claim_text"), str) for c in self.claims):
                raise ValueError("original claim extraction returned an invalid claim list")
            self.stage = "claims"
            return self._next()
        action, number = candidate["id"].split(":")
        index = int(number)
        if index in self.checked:
            raise ValueError("claim occurrence already completed")
        if action == "verify":
            result = self.context.invoke(self.config["verify_unit_id"], {"claim": self.claims[index]})
            self.results.append(result)
            self.verified_indices.add(index)
        elif action != "omit" or self.mode != "fine_optional":
            raise ValueError("original mandatory claim work cannot be skipped")
        self.checked.add(index)
        return self._next()

    def _terminal(self):
        if not isinstance(self.results, list):
            raise ValueError("original verifier did not return a verdict list")
        unchecked = ([dict(occurrence=i, claim=copy.deepcopy(c)) for i, c in enumerate(self.claims)
                      if i not in self.verified_indices]
                     if self.claims is not None else None)
        metrics = dict(verified_claims=len(self.results),
            extracted_claims=len(self.claims) if self.claims is not None else dict(status="UNKNOWN", reason="whole/skip path did not expose extraction"),
            independent_quality=dict(status="UNKNOWN", reason="requires independent task references or user-supplied teacher criterion"))
        return dict(kind="terminal", terminated=True, truncated=False, response=copy.deepcopy(self.results),
            program_metrics=metrics, evaluation_metrics=copy.deepcopy(metrics),
            evidence=dict(turn=copy.deepcopy(self.turn), claims=copy.deepcopy(self.claims),
                verification_results=copy.deepcopy(self.results), unchecked_claims=copy.deepcopy(unchecked),
                verified_occurrences=sorted(self.verified_indices) if self.claims is not None else None,
                task_policy=dict(granularity=self.mode, partial_policy_basis=self.config.get("partial_policy_basis"))),
            reason="Declared verification task completed; verdicts are not independent truth")


def make_scene(config, context):
    return PaperGraphScene(config, context)
