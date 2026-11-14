"""Score a static, redacted link review using native historical source labels."""

import json

from seampact.office_summedits import score_summedits


def score_office_links(reference, task_dict):
    """Reuse SummEdits decision semantics; approve is the equivalent of accept."""
    task = dict(task_dict)
    response = task.get("final_response")
    try:
        parsed = json.loads(response) if isinstance(response, str) else response
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and set(parsed) == {"decision"} and parsed["decision"] == "approve":
        task["final_response"] = {"decision": "accept"}
    # SummEdits accepts 'accept', but this task accepts only approve.
    elif isinstance(parsed, dict) and parsed.get("decision") == "accept":
        task["final_response"] = None
    result = score_summedits(reference, task)
    result["metrics"]["phishing_missed"] = result["metrics"]["wrong_release"]
    result["evaluator"] = "phiusiil-office-link-review-final-decision/1"
    return result
