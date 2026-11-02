"""Real evidence delivery with a bound original retriever and explicit references.

This is an evidence retrieval task, not a reproduction of REX's RL generator or
an official PaperGraph answer score. Source references are opened only when
scoring an actual delivered evidence set, including the skip branch.
"""

import copy
import re


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _project(value, path):
    """Project an explicit native result port without calling source helpers."""
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value[key]
        elif isinstance(value, (list, tuple)) and type(key) is int and key >= 0:
            value = value[key]
        else:
            raise ValueError("retrieval result does not match its declared port")
    return value


class RetrievalScene:
    def __init__(self, config, context):
        self.config, self.context = config, context
        self.unit_id = config["unit_id"]
        self.arguments = copy.deepcopy(config.get("arguments", {}))
        self.result_binding = copy.deepcopy(config.get("result_binding", {}))
        if not isinstance(self.arguments, dict) or not isinstance(self.result_binding, dict):
            raise ValueError("retrieval arguments and result_binding must be objects")
        if set(self.result_binding) - {"documents_path", "document_path", "error_path"}:
            raise ValueError("unknown retrieval result binding")
        for path in self.result_binding.values():
            if not isinstance(path, list) or any(not isinstance(k, str) and
                    not (type(k) is int and k >= 0) for k in path):
                raise ValueError("retrieval result paths require dictionary keys or nonnegative indices")
        if type(config.get("query_batch", False)) is not bool:
            raise ValueError("query_batch must be boolean")
        self.threshold = config.get("simple_query_coverage")
        if self.threshold is not None and (type(self.threshold) not in (int, float) or not 0 <= self.threshold <= 1):
            raise ValueError("simple_query_coverage must be frozen in [0,1]")
        self.required = config.get("required_units", [])
        if set(self.required) - {self.unit_id}:
            raise ValueError("retrieval obligations may reference only its bound Unit")

    def reset(self, task, seed):
        self.questions = copy.deepcopy(task["questions"])
        if not self.questions or any(not isinstance(q.get("query"), str) or not q["query"].strip()
                                     for q in self.questions):
            raise ValueError("retrieval scene requires actual nonempty source questions")
        self.documents = copy.deepcopy(task.get("existing_documents", []))
        self.position, self.deliveries = 0, []
        return self._decision()

    def _decision(self):
        question = self.questions[self.position]["query"]
        query = _tokens(question)
        observed = _tokens(" ".join(str(d.get("contents", d.get("text", ""))) for d in self.documents))
        self.coverage = len(query & observed) / len(query) if query else 0.0
        call = "retrieve:" + self.unit_id
        return dict(kind="decision", event="evidence_requested", required_units=list(self.required),
            scene_observation=dict(modalities=["text"], scopes=["paper_qa"],
                available_inputs=["question", "corpus", "paper_evidence"], question_chars=len(question),
                answer_chars=None, evidence_count=len(self.documents), answer_version=None,
                prior_calls=len(self.context.calls), remaining_budget=len(self.questions) - self.position,
                risk=None, query_token_coverage=self.coverage),
            candidates=[dict(id="continue", units=[], legal=True, features=[]),
                        dict(id=call, units=[self.unit_id], legal=True, features=[], coverage_text=question)],
            references=dict(required_only=call if self.required else "continue", all_applicable=call,
                simple=(call if self.required or self.coverage < self.threshold else "continue")
                    if self.threshold is not None else None))

    def step(self, candidate):
        question = self.questions[self.position]
        retrieved = []
        if candidate["units"]:
            arguments = copy.deepcopy(self.arguments)
            arguments[self.config.get("query_argument", "query")] = (
                [question["query"]] if self.config.get("query_batch", False) else question["query"])
            result = self.context.invoke(self.unit_id, arguments)
            if "error_path" in self.result_binding and _project(result, self.result_binding["error_path"]) is not None:
                raise ValueError("native retrieval returned an error instead of documents")
            retrieved = _project(result, self.result_binding.get("documents_path", []))
            if "document_path" in self.result_binding:
                if not isinstance(retrieved, list):
                    raise ValueError("native retrieval document port must be a list")
                retrieved = [_project(item, self.result_binding["document_path"]) for item in retrieved]
            if not isinstance(retrieved, list) or any(not isinstance(d, dict) or "id" not in d for d in retrieved):
                raise ValueError("retrieval scene requires actual returned documents with source IDs")
            by_id = {str(d["id"]): d for d in self.documents}
            by_id.update({str(d["id"]): copy.deepcopy(d) for d in retrieved})
            self.documents = list(by_id.values())
        delivered = copy.deepcopy(self.documents)
        self.deliveries.append(dict(query=question["query"], selected_action=candidate["id"],
            did_retrieve=bool(candidate["units"]),
            retrieved_documents=copy.deepcopy(retrieved), delivered_documents=delivered,
            pre_query_coverage=self.coverage))
        self.position += 1
        if self.position < len(self.questions):
            return self._decision()
        recalls, local_recalls = [], []
        for question, delivery in zip(self.questions, self.deliveries):
            reference = question.get("reference_doc_ids")
            if not isinstance(reference, list) or not reference:
                raise ValueError("independent source reference_doc_ids are required for this evidence workload")
            expected = set(map(str, reference))
            actual = {str(d["id"]) for d in delivery["delivered_documents"]}
            recalls.append(len(actual & expected) / len(expected))
            if delivery["did_retrieve"]:
                retrieved = {str(d["id"]) for d in delivery["retrieved_documents"]}
                local_recalls.append(len(retrieved & expected) / len(expected))
        metrics = dict(evidence_recall=sum(recalls) / len(recalls),
            evidence_complete=sum(v == 1 for v in recalls) / len(recalls),
            retrieved_reference_recall=sum(local_recalls) / len(local_recalls) if local_recalls else
                dict(status="UNKNOWN", reason="no retrieval was executed"),
            delivered_questions=len(recalls))
        return dict(kind="terminal", terminated=True, truncated=False, response=copy.deepcopy(self.deliveries),
            program_metrics=metrics, evaluation_metrics=copy.deepcopy(metrics),
            evidence=dict(deliveries=copy.deepcopy(self.deliveries),
                task_goal="Deliver supporting evidence for each actual paper question",
                reference_scope="source KC-to-evidence identities; references excluded from policy and teacher evidence"),
            reason="All source questions received their actual evidence delivery")


def make_scene(config, context):
    return RetrievalScene(config, context)
