"""Shared host guard interfaces and source-backed compatibility checks."""

from seampact.capability_contract import GUARD_SUBJECTS
from seampact.office_policy import ROLE_KIND, applicable_subject


def fact_check_status(output, reference_count):
    """Only a nonempty, complete per-reference result supports a conclusion."""
    if not isinstance(output, dict) or output.get("status") != "ok" or reference_count < 1:
        return "unknown"
    claims = output.get("claims")
    if (not isinstance(claims, list) or not claims or type(output.get("claim_count")) is not int
            or output["claim_count"] != len(claims)):
        return "unknown"
    labels = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_text"), str) or not claim["claim_text"].strip():
            return "unknown"
        per_reference = claim.get("labels_by_reference")
        allowed = {"Entailment", "Neutral", "Contradiction"}
        if (not isinstance(per_reference, list) or len(per_reference) != reference_count
                or any(not isinstance(label, str) or label not in allowed for label in per_reference)
                or not isinstance(claim.get("label"), str) or claim["label"] not in allowed):
            return "unknown"
        merged = ("Entailment" if "Entailment" in per_reference else
                  "Contradiction" if "Contradiction" in per_reference else "Neutral")
        if claim["label"] != merged:
            return "unknown"
        labels.append(claim["label"])
    return "supported" if all(label == "Entailment" for label in labels) else "unsupported"


def self_check_allowed(output):
    if not isinstance(output, dict) or output.get("failed") or output.get("check_status") == "unavailable":
        raise ValueError("安全自检执行失败或判定不可用")
    if type(output.get("is_blocked")) is bool:
        allowed = not output["is_blocked"]
        if output.get("decision") != ("allow" if allowed else "block"):
            raise ValueError("安全自检返回了不一致的判定")
        return allowed
    raise ValueError("安全自检未取得明确判定")


def verification_error(capability, arguments, result):
    """Validate only declared check results; business outcomes may be negative."""
    guard = capability.get("guard") or {}
    kind = guard.get("kind")
    output = result.get("output") if isinstance(result, dict) else None
    if kind == "fact_support":
        references = arguments.get("references") if isinstance(arguments, dict) else None
        count = len(references) if isinstance(references, list) else 0
        if fact_check_status(output, count) == "unknown":
            reason = output.get("unavailable_reason") if isinstance(output, dict) else None
            safe_reason = reason if isinstance(reason, str) and reason in {"truncated", "empty_output", "invalid_format", "transport_error",
                                                "no_claims", "incomplete_result", "missing_finish_reason",
                                                "invalid_response"} else "incomplete_result"
            return "事实检查首调未取得有效判定：" + safe_reason
    elif kind in {"input_safety", "output_safety"}:
        try:
            self_check_allowed(output)
        except ValueError as error:
            return str(error)
    return None

# Host-supported semantic guard interfaces.
GUARD_INTERFACES = {
    "sanitize": ({"text"}, {"sanitized_text", "was_modified"}),
    "injection": ({"prompt"}, {"valid", "risk_score"}),
    "privacy": ({"text", "entities", "anonymize"}, {"findings"}),
    "secrets": ({"text", "filename"}, {"detected"}),
    "code_scan": ({"files"}, {"issues"}),
    "self_check_input": ({"user_message"}, {"decision", "is_blocked", "failed"}),
    "self_check_output": ({"bot_message", "user_message"}, {"decision", "is_blocked", "failed"}),
    "fact_check": ({"question", "answer", "references"}, {"status", "claims", "claim_count"}),
}

_INPUT_TYPES = {
    "sanitize": {"text": "string"}, "injection": {"prompt": "string"},
    "privacy": {"text": "string", "entities": "array", "anonymize": "boolean"},
    "secrets": {"text": "string", "filename": "string"}, "code_scan": {"files": "array"},
    "self_check_input": {"user_message": "string"},
    "self_check_output": {"bot_message": "string", "user_message": "string"},
    "fact_check": {"question": "string", "answer": "string", "references": "array"},
}
_OUTPUT_TYPES = {
    "sanitize": {"sanitized_text": "string", "was_modified": "boolean"},
    "injection": {"valid": "boolean", "risk_score": ("number", "integer")},
    "privacy": {"findings": "array"}, "secrets": {"detected": "boolean"},
    "code_scan": {"issues": "array"},
    "self_check_input": {"decision": "string", "is_blocked": "boolean", "failed": "boolean"},
    "self_check_output": {"decision": "string", "is_blocked": "boolean", "failed": "boolean"},
    "fact_check": {"status": "string", "claims": "array", "claim_count": "integer"},
}
_MOUNTS = {"sanitize": ("input_received", "after_tool"), "injection": ("input_received", "after_tool"),
           "privacy": ("before_tool", "before_delivery"), "secrets": ("before_tool", "before_delivery"),
           "code_scan": ("before_tool",), "self_check_input": ("input_received", "before_tool"),
           "self_check_output": ("before_delivery",), "fact_check": ("before_tool", "before_delivery")}


def _typed_fields(properties, expected):
    return all(isinstance(properties.get(name), dict) and
               (properties[name].get("type") in kind if isinstance(kind, tuple)
                else properties[name].get("type") == kind) for name, kind in expected.items())


def _semantic_contract(role, guard, inputs, output):
    in_fields, out_fields = inputs.get("properties", {}), output.get("properties", {})
    if not isinstance(in_fields, dict) or not isinstance(out_fields, dict):
        return False
    required_input, required_output = inputs.get("required", []), output.get("required", [])
    expected_output = _OUTPUT_TYPES[role]
    if (not isinstance(required_input, list) or not isinstance(required_output, list)
            or any(not isinstance(name, str) for name in required_input + required_output)
            or set(required_input) - _INPUT_TYPES[role].keys()
            or not set(expected_output) <= set(required_output)
            or not _typed_fields(in_fields, _INPUT_TYPES[role]) or not _typed_fields(out_fields, expected_output)
            or not any(applicable_subject(role, event, guard) for event in _MOUNTS[role])):
        return False
    if role == "privacy":
        items = in_fields["entities"].get("items")
        if not isinstance(items, dict) or items.get("type") != "string":
            return False
    if role == "code_scan":
        items = in_fields["files"].get("items", {})
        props = items.get("properties", {}) if isinstance(items, dict) else {}
        required = items.get("required", []) if isinstance(items, dict) else []
        if (not isinstance(items, dict) or items.get("type") != "object" or not isinstance(props, dict)
                or not _typed_fields(props, {"name": "string", "source": "string"})
                or not isinstance(required, list) or any(not isinstance(name, str) for name in required)
                or set(required) - {"name", "source"}):
            return False
        issue = out_fields["issues"].get("items", {})
        issue_fields = issue.get("properties", {}) if isinstance(issue, dict) else {}
        issue_required = issue.get("required", []) if isinstance(issue, dict) else []
        if not isinstance(issue, dict) or issue.get("type") != "object" or not isinstance(issue_fields, dict) or not any(
                isinstance(issue_required, list) and field in issue_required and
                _typed_fields(issue_fields, {field: "string"}) for field in ("issue_severity", "severity")):
            return False
    return True


def _source_backed(descriptor):
    if not isinstance(descriptor, dict):
        return False
    references = descriptor.get("provenance")
    return (isinstance(references, list) and bool(references) and all(
        isinstance(ref, dict) and isinstance(ref.get("path"), str) and bool(ref["path"])
        and type(ref.get("start")) is int and type(ref.get("end")) is int
        and 1 <= ref["start"] <= ref["end"] for ref in references))


def compatible(role, unit):
    if role not in GUARD_INTERFACES:
        return False
    inputs, outputs = GUARD_INTERFACES[role]
    capability = unit.get("capability", {})
    if not isinstance(capability, dict):
        return False
    output = capability.get("output", {})
    if not isinstance(output, dict):
        return False
    output = output.get("schema", output)
    input_schema = capability.get("input", {})
    if not isinstance(output, dict) or not isinstance(input_schema, dict):
        return False
    input_fields, output_fields = input_schema.get("properties", {}), output.get("properties", {})
    if not isinstance(input_fields, dict) or not isinstance(output_fields, dict):
        return False
    guard = capability.get("guard")
    identity_matches = (_source_backed(guard) and guard.get("kind") == ROLE_KIND[role]
                        and isinstance(guard.get("subjects"), list) and bool(guard["subjects"])
                        and all(isinstance(subject, str) and subject in GUARD_SUBJECTS for subject in guard["subjects"])
                        and (role != "fact_check" or guard.get("evidence") == "references")
                        if isinstance(guard, dict) else False)
    if not (identity_matches and inputs <= set(input_fields) and outputs <= set(output_fields)):
        return False
    if guard is not None and not _semantic_contract(role, guard, input_schema, output):
        return False
    if role != "fact_check":
        return True
    claims = output_fields["claims"]
    fields = claims.get("items", {}).get("properties", {})
    return (set(inputs) <= set(input_schema.get("required", []))
            and input_fields["question"].get("type") == "string"
            and input_fields["answer"].get("type") == "string"
            and input_fields["references"].get("type") == "array"
            and input_fields["references"].get("items", {}).get("type") == "string"
            and output_fields["status"].get("type") == "string"
            and output_fields["claim_count"].get("type") == "integer"
            and claims.get("type") == "array"
            and fields.get("claim_text", {}).get("type") == "string"
            and fields.get("label", {}).get("type") == "string"
            and fields.get("labels_by_reference", {}).get("type") == "array"
            and fields["labels_by_reference"].get("items", {}).get("type") == "string")


def validate_guard_contract(capability):
    """Reject declared guards the host cannot mount, while allowing normal tools."""
    guard = capability.get("guard")
    if guard is None:
        return
    roles = [role for role, kind in ROLE_KIND.items() if kind == guard.get("kind")]
    if not any(compatible(role, {"capability": capability}) for role in roles):
        contracts = "; ".join(
            f"{role}: input={_INPUT_TYPES[role]}, output={_OUTPUT_TYPES[role]}, events={_MOUNTS[role]}"
            for role in roles)
        raise ValueError("检查能力 " + str(capability.get("id")) + " 的标准输入、输出或检查对象不匹配宿主接口；"
                         "请修订适配器及清单：" + contracts)
