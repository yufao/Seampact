"""Score a frozen SummEdits fact-checking task without seeing agent internals."""

import json


def score_summedits(reference, task_dict):
    """Score a final JSON decision against the original binary annotation.

    Source label 1 means factually consistent; 0 means inconsistent.  The
    task asks for accept/reject/abstain. An abstention is observable but does
    not complete the requested decision. An unparseable final is unknown.
    Incomplete runs have no delivered decision, even if a partial exists.
    """
    label = reference.get("label")
    if type(label) is not int or label not in (0, 1):
        raise ValueError("SummEdits reference must contain native label 0 or 1")
    completed = task_dict.get("status") == "COMPLETED"
    decision = None
    if completed:
        response = task_dict.get("final_response")
        try:
            parsed = json.loads(response) if isinstance(response, str) else response
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and set(parsed) == {"decision"}:
            value = parsed["decision"]
            if value in ("accept", "reject", "abstain"):
                decision = value

    known = decision is not None
    correct = known and decision == ("accept" if label == 1 else "reject")
    quality = float(correct) if known else (0.0 if not completed else None)
    metrics = {
        "legitimate_completion": int(correct) if known else (0 if not completed else None),
        "wrong_release": int(decision == "accept") if label == 0 and known else None,
        "false_rejection": int(decision == "reject") if label == 1 and known else None,
        "abstained": int(decision == "abstain") if known else None,
        "decision_unknown": int(not known),
        "business_completed": int(completed),
    }
    return {"quality": quality, "metrics": metrics, "evaluator": "summedits-office-final-decision/1",
            "status": "UNKNOWN" if quality is None else "EVALUATED", "terminal_status": task_dict.get("status")}
