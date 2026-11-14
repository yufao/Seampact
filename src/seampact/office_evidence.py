"""Bounded references from tool results still visible to the business model."""

import hashlib
import json
import copy
import datetime
import math
import re
from collections import deque
from itertools import islice, zip_longest

MAX_REFERENCES = 6
MAX_REFERENCE_CHARS = 3000
MAX_TOTAL_CHARS = 12000
MAX_OBSERVATIONS = 64
MAX_JSON_CHARS = 256000
REFERENCE_FIELDS = frozenset({"content", "text", "body", "message", "summary"})


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate structured key")
        result[key] = value
    return result


def _structured(content):
    """Parse bounded JSON or safe YAML, rejecting aliases and ambiguous keys."""
    if not isinstance(content, str) or len(content) > MAX_JSON_CHARS:
        return None
    try:
        value = json.loads(content, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, TypeError, RecursionError):
        # YAML is optional for the lean host; without it the exact visible
        # text remains available to NLI and never becomes a direct proof.
        if not re.match(r"\s*(?:-\s+|[\w.-]+\s*:\s*)", content):
            return None
        try:
            import yaml
            if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken, yaml.TagToken)) for token in yaml.scan(content)):
                return None
            class UniqueLoader(yaml.SafeLoader):
                def construct_mapping(self, node, deep=False):
                    return _unique_pairs((self.construct_object(key, deep=deep),
                                          self.construct_object(value, deep=deep)) for key, value in node.value)
            value = yaml.load(content, Loader=UniqueLoader)
        except Exception:
            return None
    if not isinstance(value, (dict, list)):
        return None
    remaining = [2048]
    def normalized(item, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 20:
            raise ValueError("structure limit")
        if item is None or type(item) in (str, bool, int):
            return item
        if type(item) is float and math.isfinite(item):
            return item
        if isinstance(item, (datetime.datetime, datetime.date)):
            return {"$type": "datetime" if isinstance(item, datetime.datetime) else "date", "value": item.isoformat()}
        if isinstance(item, list):
            return [normalized(child, depth + 1) for child in item]
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            return {key: normalized(child, depth + 1) for key, child in item.items()}
        raise ValueError("unsupported structured type")
    try:
        return normalized(value)
    except (ValueError, RecursionError):
        return None


def _same_value(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _unambiguous_structure(value):
    if isinstance(value, list):
        # A collection of records may require knowing which one answers the
        # request; exact value matching alone does not resolve that choice.
        if len(value) > 1 and any(isinstance(item, (dict, list)) for item in value):
            return False
        return all(_unambiguous_structure(item) for item in value)
    if isinstance(value, dict):
        return all(_unambiguous_structure(item) for item in value.values())
    return True


def _fingerprint(content):
    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).digest()


def _passages(content):
    """Yield only prose fields; a host preview is not a complete source result."""
    if not isinstance(content, str) or not content.strip():
        return
    stripped = content.lstrip()
    if len(content) > MAX_JSON_CHARS and stripped.startswith(("{", "[", '"')):
        return
    try:
        value = json.loads(content) if len(content) <= MAX_JSON_CHARS else content
    except (ValueError, TypeError, RecursionError):
        value = content

    def collect(item, textual=False):
        if isinstance(item, str):
            if textual and item.strip():
                yield item
        elif isinstance(item, list):
            for child in item:
                yield from collect(child, textual)
        elif isinstance(item, dict):
            for key, child in item.items():
                yield from collect(child, key in REFERENCE_FIELDS)

    try:
        yield from collect(value, isinstance(value, str))
    except RecursionError:
        return


def visible_reference_texts(content):
    """Return at most six bounded passages from the supplied visible content."""
    return [text[:MAX_REFERENCE_CHARS] for text in islice(_passages(content), MAX_REFERENCES)]


def _preview(content):
    """Recognize the runtime's result preview without treating it as a source."""
    if not isinstance(content, str) or len(content) > MAX_JSON_CHARS:
        return False
    try:
        value = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return False
    marker = value.get("truncated") if isinstance(value, dict) else None
    return (isinstance(value, dict) and (marker is True or marker == "true") and "preview" in value)


def _receipt(payload):
    name, call_id, arguments, content = (payload.get(k) for k in ("name", "call_id", "arguments", "content"))
    if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id or not isinstance(arguments, dict):
        return None, False
    if not isinstance(content, str):
        return None, False
    def identity(value):
        return ((isinstance(value, str) and len(value) <= 200 and
                 re.fullmatch(r"[\w.@:/+~\-]+", value) is not None)
                or type(value) is int and 0 <= value <= 10**18)
    def relative_path(value):
        if (not isinstance(value, str) or not value or len(value) > 200 or value.startswith("/")
                or not re.fullmatch(r"[\w.@/+~\-]+", value)):
            return False
        return all(part not in {"", ".", ".."} for part in value.split("/"))
    if not identity(name) or not identity(call_id):
        return None, True
    target_keys = ("file_id", "path", "filename", "channel_id", "channel", "recipient", "to", "destination")
    target = {key: arguments[key] for key in target_keys if
              (relative_path(arguments.get(key)) if key in {"path", "filename"} else identity(arguments.get(key)))}
    base = dict(evidence_type="executed_operation_receipt", executed_tool=name, call_id=call_id)
    if target:
        base["target"] = target
        base["target_basis"] = "requested_arguments"
    if len(content) <= MAX_JSON_CHARS and not _preview(content):
        try:
            value = json.loads(content)
        except (ValueError, TypeError, RecursionError):
            value = None
        # Only recognize the host's common structured result wrappers. Never
        # copy arbitrary response fields or an echoed payload into evidence.
        if isinstance(value, dict):
            for key in ("result", "output"):
                if isinstance(value.get(key), dict):
                    value = value[key]
            ids = {key: value[key] for key in ("id", "receipt_id", "message_id", "file_id")
                   if identity(value.get(key))}
            status = value.get("status")
            if isinstance(status, str) and status.lower() in {"ok", "success", "sent", "created", "updated",
                                                               "deleted", "delivered", "failed", "error"}:
                ids["status"] = status
            transport = value.get("transport")
            if isinstance(transport, str) and transport.lower() in {"simulated", "mock", "local", "smtp", "http", "https"}:
                ids["transport"] = transport
            for key in ("workspace_path", "artifact_path", "file_path", "path"):
                if relative_path(value.get(key)):
                    ids[key] = value[key]
            if ids:
                base["reported"] = ids
                base["reported_basis"] = "tool_return_only"
    skipped_json = len(content) > MAX_JSON_CHARS and content.lstrip().startswith(("{", "["))
    return json.dumps(base, ensure_ascii=False), skipped_json


def _workspace_stdout(payload):
    """Retain only an actual visible, complete host workspace execution output."""
    name, call_id, content = (payload.get(key) for key in ("name", "call_id", "content"))
    if (not isinstance(name, str) or not name or len(name) > 200 or
            not isinstance(call_id, str) or not call_id or len(call_id) > 200 or
            not isinstance(content, str)):
        return None, True
    if payload.get("is_error") or len(content) > MAX_JSON_CHARS or _preview(content):
        return None, True
    try:
        wrapper = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return None, True
    result = wrapper.get("result") if isinstance(wrapper, dict) else None
    if (not isinstance(result, dict) or result.get("status") != "COMPLETED"
            or type(result.get("exit_code")) is not int or result["exit_code"] != 0
            or result.get("stdout_truncated") is not False or not isinstance(result.get("stdout"), str)):
        return None, True
    stdout = result["stdout"]
    if not stdout.strip():
        return None, False
    evidence = json.dumps(dict(evidence_type="workspace_execution_stdout", executed_tool=name,
                               call_id=call_id, stdout=stdout), ensure_ascii=False)
    if len(evidence) > MAX_REFERENCE_CHARS:
        return None, True
    return evidence, False


class EvidenceWindow:
    """Track references by live call identity and exact visible content.

    Descriptors retain hashes rather than whole results; passage and descriptor
    counts are capped independently of the number or size of tool outputs.
    """

    def __init__(self):
        self._observations = deque(maxlen=MAX_OBSERVATIONS)
        self._evicted = deque(maxlen=MAX_OBSERVATIONS)
        self._references = []
        self._structured_results = []
        self._visible_records = []
        self._source_count = self._receipt_count = 0
        self._partial = False

    def observe(self, payload, kind):
        execution = kind == "execute" and payload.get("tool_source") == "workspace"
        if not execution and (kind not in {"read", "retrieve", "metadata", "write", "send"} or payload.get("is_error")):
            return
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        preview = _preview(content)
        receipt = execution or kind in {"write", "send"}
        partial = preview
        structure, structure_text, observed_record = None, None, None
        if execution:
            text, bounded = _workspace_stdout(payload)
            passages = [text] if text else []
            partial |= bounded
            if text:
                observed_record = dict(kind="execute", tool=payload.get("name"), call_id=payload.get("call_id"),
                                       operation=kind, origin="workspace", result=json.loads(text)["stdout"],
                                       scope="visible_stdout")
        elif receipt:
            text, bounded = _receipt(payload)
            passages = [text] if text else []
            partial |= bounded
            returned = _structured(content)
            if returned is not None and len(content) <= MAX_REFERENCE_CHARS:
                observed_record = dict(kind="tool_report", tool=payload.get("name"), call_id=payload.get("call_id"),
                    operation=kind, origin=payload.get("tool_source"), result=returned,
                    scope="tool_report_only")
        elif preview or len(content) > MAX_JSON_CHARS and content.lstrip().startswith(("{", "[", '"')):
            passages, partial = [], True
        elif kind == "metadata":
            name, call_id = payload.get("name"), payload.get("call_id")
            if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
                return
            value = _structured(content)
            value = value if value is not None else content
            observed_record = dict(kind="metadata", tool=name, call_id=call_id, operation=kind,
                                   origin=payload.get("tool_source"), result=value, scope="visible_result")
            structure_text = json.dumps(dict(evidence_type="visible_tool_result", tool=name, call_id=call_id,
                operation=kind, origin=payload.get("tool_source"), value=value), ensure_ascii=False)
            passages = [structure_text] if len(structure_text) <= MAX_REFERENCE_CHARS else []
            partial |= not bool(passages)
            if not passages:
                observed_record = None
        else:
            raw = list(islice(_passages(content), MAX_REFERENCES + 1))
            partial |= len(raw) > MAX_REFERENCES or any(len(text) > MAX_REFERENCE_CHARS for text in raw)
            passages = list(dict.fromkeys(text[:MAX_REFERENCE_CHARS] for text in raw[:MAX_REFERENCES]))
            value = _structured(content)
            if (isinstance(value, dict) and "archive_ref" in value and "result" in value
                    and set(value) <= {"archive_ref", "result"}):
                value = value["result"]
            if isinstance(value, (dict, list)) and value:
                name, call_id = payload.get("name"), payload.get("call_id")
                if isinstance(name, str) and name and isinstance(call_id, str) and call_id:
                    # Only strict JSON is eligible for direct exact-result
                    # proof; YAML dates and scalars need the NLI path.
                    structure = value if content.lstrip().startswith(("{", "[")) else None
                    observed_record = dict(kind="read", tool=name, call_id=call_id, operation=kind,
                                           origin=payload.get("tool_source"), result=value, scope="visible_result")
                    structure_text = json.dumps(dict(evidence_type="visible_tool_result", tool=name, call_id=call_id,
                        operation=kind, origin=payload.get("tool_source"), value=value), ensure_ascii=False)
                    if len(structure_text) <= MAX_REFERENCE_CHARS:
                        if (isinstance(value, dict) and set(value) == {"content"}
                                and isinstance(value["content"], str) and passages == [value["content"]]):
                            # A single prose value needs no duplicate copy.
                            structure_text = passages[0]
                        else:
                            passages = [structure_text]
                            partial = preview
                    else:
                        structure, structure_text, observed_record, partial = None, None, None, True
        if not passages and not partial:
            return
        if len(self._observations) == MAX_OBSERVATIONS:
            oldest = self._observations[0]
            self._evicted.append((oldest["call_id"], oldest["fingerprint"]))
        self._observations.append(dict(call_id=payload.get("call_id"), fingerprint=_fingerprint(content),
                                       passages=passages, receipt=receipt, partial=partial,
                                       structure=structure, structure_text=structure_text, record=observed_record))

    def retain(self, model_context_payload):
        visible = {(item.get("call_id"), _fingerprint(item["content"]))
                   for item in (model_context_payload.get("tool_messages") or [])
                   if isinstance(item, dict) and isinstance(item.get("call_id"), str)
                   and item["call_id"] and isinstance(item.get("content"), str)}
        retained = [item for item in self._observations if isinstance(item["call_id"], str)
                    and item["call_id"] and (item["call_id"], item["fingerprint"]) in visible]
        evicted = [item for item in self._evicted if item in visible]
        self._observations = deque(retained, maxlen=MAX_OBSERVATIONS)
        self._evicted = deque(evicted, maxlen=MAX_OBSERVATIONS)
        references, seen, structures = [], set(), []
        self._partial = bool(evicted) or any(item["partial"] for item in retained)
        source_count = receipt_count = 0
        used = 0
        for rank in zip_longest(*([(text, item) for text in item["passages"]] for item in reversed(retained))):
            for pair in rank:
                if not pair:
                    continue
                text, owner = pair
                if not text or text in seen:
                    continue
                seen.add(text)
                if len(references) >= MAX_REFERENCES or used + len(text) > MAX_TOTAL_CHARS:
                    self._partial = True
                    continue
                receipt_count += int(owner["receipt"])
                source_count += int(not owner["receipt"])
                references.append(text)
                if owner.get("structure") is not None and text == owner.get("structure_text"):
                    structures.append(owner["structure"])
                used += len(text)
        self._references = references
        self._structured_results = structures
        self._visible_records = [item["record"] for item in retained if item.get("record") is not None]
        self._source_count, self._receipt_count = source_count, receipt_count

    @property
    def references(self):
        return list(self._references)

    @property
    def source_count(self):
        return self._source_count

    @property
    def receipt_count(self):
        return self._receipt_count

    @property
    def partial(self):
        return self._partial

    def proves_complete_structured_answer(self, answer):
        """Prove only exact representation of one unambiguous visible read."""
        if not isinstance(answer, str) or not answer.lstrip().startswith(("{", "[")):
            return False
        value = _structured(answer)
        return (bool(value) and _unambiguous_structure(value) and len(self._structured_results) == 1
                and _same_value(value, self._structured_results[0]))

    @property
    def visible_records(self):
        """Typed tool reports in current context; reports do not assert effects."""
        return copy.deepcopy(self._visible_records)

    def facts(self):
        return dict(linked_source=bool(self.source_count), linked_receipt=bool(self.receipt_count),
                    missing_evidence=not bool(self.references), evidence_partial=self.partial)
