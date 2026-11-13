"""Office protection configuration over generated capabilities and host hooks.

This is an application, not a second host or a replacement of source algorithms.
Dataset identities and hidden scores never enter applicability or decisions.
"""

import copy
import json
import re
from types import SimpleNamespace

from seampact.agent_runtime import AgentHook
from seampact.providers import RequestCancelled


OUTBOUND = frozenset({"send_email", "send_slack_message", "send_direct_message", "send_channel_message", "post_message",
                      "deliver_simulated_receipt"})
PRIVATE_ENTITIES = ["CREDIT_CARD", "US_SSN", "IBAN_CODE", "CRYPTO"]
METADATA_TOOLS = frozenset({"get_channels", "get_current_day", "list_files", "workspace_list_files",
                            "list_drive_file_metadata", "get_users_in_channel"})
TEXT_FIELDS = frozenset({"content", "body", "text", "message", "description", "subject", "title",
                         "summary", "instructions", "details"})
SUSPICIOUS = re.compile(r"ignore.{0,35}(?:previous|instruction)|system\s*(?:message|prompt)|"
                       r"(?:password|api[_ -]?key|secret)\s*[:=]|[\u200b-\u200f\ufeff]", re.I | re.S)

# Only actual reading/retrieval results and executed tool receipts become references. Tool observations
# arrive after all output guards and the host's visibility/truncation decision.
REFERENCE_TOOLS = frozenset({"read_file", "workspace_read_file", "get_file_by_id", "get_email",
                             "search_emails", "get_thread", "get_message", "search_files"})
REFERENCE_FIELDS = frozenset({"content", "text", "body", "message", "summary"})


def visible_reference_texts(content):
    if not isinstance(content, str) or not content.strip():
        return []
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return [content]
    def collect(item, textual=False):
        if isinstance(item, str):
            return [item] if textual and item.strip() else []
        if isinstance(item, list):
            return [text for child in item for text in collect(child, textual)]
        if isinstance(item, dict):
            return [text for key, child in item.items()
                    for text in collect(child, key in REFERENCE_FIELDS)]
        return []
    return collect(value, isinstance(value, str))


def fact_check_status(output, reference_count):
    """Only a nonempty, complete per-reference result can support a conclusion."""
    if not isinstance(output, dict) or output.get("status") != "ok":
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
    if output.get("failed"):
        raise ValueError("安全自检执行失败")
    if type(output.get("is_blocked")) is bool:
        allowed = not output["is_blocked"]
        if output.get("decision") != ("allow" if allowed else "block"):
            raise ValueError("安全自检返回了不一致的判定")
        return allowed
    if type(output.get("allowed")) is bool:
        return output["allowed"]
    raise ValueError("安全自检未取得明确判定")


def payload_text(event, payload):
    if event == "input_received":
        return "\n".join(m.get("content", "") for m in payload.get("messages", [])
                         if isinstance(m.get("content"), str))
    if event == "before_delivery":
        return payload.get("text", "")
    if event == "after_tool" and payload.get("name") in METADATA_TOOLS:
        return ""
    value = payload.get("arguments" if event == "before_tool" else "result")
    if isinstance(value, dict) and "content" in value:
        value = value["content"]
    if isinstance(value, dict) and "output" in value:
        value = value["output"]
    if event == "after_tool":
        # Text classifiers consume source prose, not the host's JSON envelope,
        # scores, document IDs or directory labels. This mapping belongs to the
        # office application and is shared by all evaluated scheduling policies.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return value
        def prose(item):
            if isinstance(item, str):
                return [item]
            if isinstance(item, list):
                return [text for child in item for text in prose(child)]
            if isinstance(item, dict):
                return [text for key, child in item.items()
                        if key in TEXT_FIELDS or isinstance(child, (dict, list)) for text in prose(child)]
            return []
        return "\n".join(prose(value))
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def observe(event, payload):
    text = payload_text(event, payload)
    checks = payload.get("checks", {})
    risks = [float(result.get("risk", 0)) for result in checks.values() if isinstance(result, dict)]
    return dict(text_chars=len(text), has_code=bool(re.search(r"```(?:python|py)\b", text)),
                has_evidence=bool(payload.get("evidence")), external_material=event == "after_tool",
                checks_run=len(checks), risk_signal=max([float(bool(SUSPICIOUS.search(text))), *risks]))


def build_hooks(platform, task_id, config):
    bindings, mode = config["bindings"], config["mode"]
    if mode not in {"app_config", "resource_rule", "ppo", "scheduler"}:
        raise ValueError("未知办公策略配置")
    hooks, scheduler_bindings, roles = [], {}, {}
    evidence_seen = [False]
    user_message = []
    observations, references = [], []

    def call(context, role, arguments):
        if role == "fact_check":
            result = context.invoke(bindings[role], arguments, private_events=True)
        else:
            result = context.invoke(bindings[role], arguments)
        output = result.get("output")
        if not isinstance(output, dict):
            raise ValueError(role + " 能力没有返回结构化判定")
        return output

    def add(role, event, handler, *, when=None):
        if role not in bindings:
            return
        name = role + ":" + event
        hooks.append(AgentHook(name, event, handler, required=False,
                               mode="complete" if event == "before_delivery" else "fragment", when=when))
        scheduler_bindings[name] = dict(unit_id=bindings[role])
        roles[name] = role

    def remember_input(payload, context):
        user_message[:] = [payload_text("input_received", payload)]
    def remember_evidence(payload, context):
        text = payload_text("after_tool", payload)
        if text and payload.get("name") not in OUTBOUND:
            evidence_seen[0] = True
    hooks.extend([AgentHook("office_input_context", "input_received", remember_input),
                  AgentHook("office_evidence_context", "after_tool", remember_evidence)])

    if "fact_check" in bindings:
        def remember_reference(payload, context):
            name = payload.get("name", "")
            if payload.get("is_error"):
                return
            if name in OUTBOUND and payload.get("call_id") and isinstance(payload.get("arguments"), dict):
                # Bind the receipt to the executed, guarded call. Never use an
                # assistant's planned action or claimed success as evidence.
                texts = [json.dumps(dict(executed_tool=name, call_id=payload["call_id"],
                    arguments=payload["arguments"], observed_result=payload.get("content")), ensure_ascii=False)]
            elif name in REFERENCE_TOOLS or name.startswith("search_documents_"):
                texts = visible_reference_texts(payload.get("content"))
            else:
                return
            for text in texts:
                if len(text) > 3000:
                    continue
                while observations and (len(observations) >= 6 or
                        sum(len(item[1]) for item in observations) + len(text) > 12000):
                    observations.pop(0)
                observations.append((payload["content"], text))
        def retain_model_context(payload, context):
            visible = payload.get("tool_contents", [])
            observations[:] = [(content, text) for content, text in observations if content in visible]
            references[:] = list(dict.fromkeys(text for content, text in observations if content in visible))
        hooks.append(AgentHook("office_visible_references", "tool_observation", remember_reference))
        hooks.append(AgentHook("office_reference_context", "model_context", retain_model_context))

        def factual_text(payload):
            if "name" not in payload:
                return payload.get("text", "")
            arguments = payload.get("arguments", {})
            return "\n".join(arguments[field] for field in ("subject", "body", "content", "message", "text")
                             if isinstance(arguments.get(field), str))

        def can_fact_check(payload):
            question, answer = "\n".join(user_message), factual_text(payload)
            return bool(references and question and answer and len(question) <= 1500 and len(answer) <= 8000)

        def fact_check(payload, context):
            question = "\n".join(user_message)
            answer = factual_text(payload)
            if not can_fact_check(payload):
                return dict(action="allow", fact_support="unknown", check_status="not_applicable")
            diagnostic = dict(scope="outbound_content" if "name" in payload else "final_delivery",
                              reference_count=len(references))
            try:
                output = call(context, "fact_check", dict(question=question, answer=answer,
                                                           references=list(references)))
                status = fact_check_status(output, len(references))
                diagnostic["check_status"] = "valid" if status != "unknown" else "unavailable"
                if status != "unknown":
                    diagnostic["claim_count"] = output["claim_count"]
                    diagnostic["label_counts"] = {label: sum(claim["label"] == label for claim in output["claims"])
                                                   for label in ("Entailment", "Neutral", "Contradiction")}
                else:
                    diagnostic["unavailable_reason"] = "incomplete_result"
            except RequestCancelled:
                raise
            except Exception as error:
                status = "unknown"
                diagnostic.update(check_status="unavailable", unavailable_reason="call_error",
                                  error_type=type(error).__name__)
            # A decision may include the aggregate support status, never the
            # private source passages or individual extracted claims.
            if status == "unsupported":
                return dict(action="block", fact_support=status, reason="参考资料未支持待交付的事实主张", **diagnostic)
            return dict(action="allow", fact_support=status, **diagnostic)
        add("fact_check", "before_delivery", fact_check, when=can_fact_check)
        add("fact_check", "before_tool", fact_check,
            when=lambda p: p.get("name") in OUTBOUND and can_fact_check(p))

    for event in ("input_received", "after_tool"):
        def sanitize(payload, context, stage=event):
            text = payload_text(stage, payload)
            out = call(context, "sanitize", dict(text=text))
            # Sanitization is confined to actual textual fields; never rewrite a
            # structured tool response by replacing its JSON serialization.
            result = dict(action="allow", risk=float(bool(out.get("was_modified"))))
            if out.get("was_modified"):
                if stage == "input_received":
                    messages = copy.deepcopy(payload["messages"])
                    if len(messages) == 1 and isinstance(messages[0].get("content"), str):
                        messages[0]["content"] = out["sanitized_text"]
                        result.update(action="replace", messages=messages)
                # Structured results keep their original fields; their prose is
                # inspected without replacing the entire response by one string.
            return result
        def injection(payload, context, stage=event):
            out = call(context, "injection", dict(prompt=payload_text(stage, payload)))
            if type(out.get("valid")) is not bool:
                raise ValueError("注入检测未返回明确判定")
            return dict(action="allow" if out["valid"] else "block", reason="检测到指令注入",
                        risk=max(0.0, min(1.0, float(out.get("risk_score", 0)))))
        add("sanitize", event, sanitize, when=lambda p, e=event: bool(payload_text(e, p)))
        add("injection", event, injection, when=lambda p, e=event: bool(payload_text(e, p)))

    def self_check(context, role, arguments):
        try:
            out = call(context, role, arguments)
            if out.get("failed") or out.get("check_status") == "unavailable":
                reasons = {"truncated", "empty_output", "invalid_format", "missing_finish_reason",
                           "incomplete", "invalid_response", "transport_error", "action_error"}
                reason = out.get("unavailable_reason")
                return dict(action="block", check_status="unavailable", reason="安全检查未完成，暂不放行",
                            unavailable_reason=reason if reason in reasons else "invalid_result")
            return dict(action="allow" if self_check_allowed(out) else "block", check_status="valid",
                        reason="安全检查阻止请求或交付")
        except RequestCancelled:
            raise
        except Exception as error:
            return dict(action="block", check_status="unavailable", reason="安全检查未完成，暂不放行",
                        unavailable_reason="call_error", error_type=type(error).__name__)

    def self_check_input(payload, context):
        return self_check(context, "self_check_input", dict(user_message=payload_text("input_received", payload)))
    add("self_check_input", "input_received", self_check_input)

    def self_check_output(payload, context):
        return self_check(context, "self_check_output", dict(bot_message=payload["text"], user_message="\n".join(user_message)))
    add("self_check_output", "before_delivery", self_check_output, when=lambda p: bool(p.get("text")))

    for event in ("before_delivery", "before_tool"):
        def privacy(payload, context, stage=event):
            out = call(context, "privacy", dict(text=payload_text(stage, payload), entities=PRIVATE_ENTITIES, anonymize=False))
            return dict(action="block" if out.get("findings") else "allow", reason="交付内容包含受保护的财务身份信息",
                        risk=float(bool(out.get("findings"))))
        def secrets(payload, context, stage=event):
            out = call(context, "secrets", dict(text=payload_text(stage, payload), filename="outbound.txt"))
            return dict(action="block" if out.get("detected") else "allow", reason="交付内容包含凭据",
                        risk=float(bool(out.get("detected"))))
        predicate = (lambda p: bool(p.get("text"))) if event == "before_delivery" else (
            lambda p: p.get("name") in OUTBOUND)
        add("privacy", event, privacy, when=predicate)
        add("secrets", event, secrets, when=predicate)

    def scan_code(payload, context):
        arguments = payload["arguments"]
        source = arguments.get("code", arguments.get("source", ""))
        if payload.get("name") == "submit_spreadsheet_solution":
            with platform.projects.lock:
                scope = platform.projects.task_scope(task_id)
                if not scope or scope.get("status") != "ACTIVE":
                    raise ValueError("检查表格解法需要有效的任务工作区")
                path = platform.projects.file_path(scope["project_id"], scope["conversation_id"], "workspace", "solution.py")
                with path.open("rb") as stream:
                    content = stream.read(131073)
                if len(content) > 131072:
                    raise ValueError("待检查的表格解法超过 128 KiB")
                source = content.decode("utf-8")
        out = call(context, "code_scan", dict(files=[dict(name="task.py", source=source)]))
        issues = out.get("issues", [])
        severe = [i for i in issues if i.get("issue_severity", i.get("severity")) == "HIGH"]
        return dict(action="block" if severe else "allow", reason="代码含高严重性风险",
                    risk=float(bool(issues)), issue_count=len(issues))
    add("code_scan", "before_tool", scan_code, when=lambda p: p.get("name") == "submit_spreadsheet_solution" or
        p.get("name") in {"run_python", "workspace_run_python"} and bool(p.get("arguments", {}).get("code")))

    def observed(event, payload):
        return dict(observe(event, payload), has_evidence=bool(references) if "fact_check" in bindings else evidence_seen[0])

    def select(event, payload, remaining):
        eligible = list(remaining)
        if mode == "resource_rule":
            facts = observed(event, payload)
            cheap = {"sanitize", "secrets", "code_scan"}
            selected = cheap | ({"injection", "self_check_input", "self_check_output"}
                                if facts["risk_signal"] else set())
            if re.search(r"\b\d[\d -]{7,}\b", payload_text(event, payload)):
                selected.add("privacy")
            if references:
                selected.add("fact_check")
            eligible = [hook for hook in eligible if roles.get(hook.name) in selected]
        if not eligible:
            return dict(kind="continue")
        return dict(kind="run_hook", hook=eligible[0].name)
    selector = (platform.execution.optional_selector(task_id, scheduler_bindings, observed)
                if mode in {"ppo", "scheduler"} else SimpleNamespace(choose_next=select))
    return dict(hooks=tuple(hooks), optional_hooks=selector, internal_tool_text=True)
