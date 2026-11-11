"""Closed value-port evidence, separate from source/SDK effect permission.

This recognizes only shapes enforced by the current native argument checker.
An untyped schema is not a JSON proof: original native references can bypass
the JSON arm. Passive literal records may be finite enum values; arbitrary
native record instances still need their own input/alias protocol.
"""

import copy
import ast
import hashlib
import json
import math

from seampact.source.keyword_domain import narrow_keywords


RULE = "closed-request-value-domain/2"
EXACT_REQUEST_CODEC = "group-port/3"
TRANSPORT_GUARDS = ("COORDINATOR_EXACT_GROUP_PORT_3_REQUEST", "NO_GROUP_NATIVE_ARGUMENT_REFERENCES")


class DomainGap(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _literal(value, depth=0):
    if depth > 8:
        raise DomainGap("finite literal domain exceeds depth 8")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _literal(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _literal(item, depth + 1)
        return
    raise DomainGap("finite enum values must be plain, finite JSON data")


def value_domain(schema, depth=0):
    """Prove a bounded *description*, not a bounded request length or SDK effect."""
    if depth > 16:
        raise DomainGap("value schema exceeds finite depth 16")
    if type(schema) is not dict or not schema:
        raise DomainGap("untyped value schema does not exclude original objects/callbacks")
    if "x-native-type" in schema:
        raise DomainGap("native object inputs lack an exact passive-value/alias guard")
    if set(schema) == {"enum"}:
        values = schema["enum"]
        if type(values) is not list or not values or len(values) > 128:
            raise DomainGap("enum requires 1 to 128 explicit literal values")
        keys = []
        for value in values:
            _literal(value)
            keys.append(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   allow_nan=False, separators=(",", ":")))
        if len(set(keys)) != len(keys):
            raise DomainGap("enum choices must be distinct under strict JSON identity")
        return dict(kind="finite_literal_values", choices=len(values),
                    contains_passive_records=any(type(v) is dict for v in values),
                    contains_lists=any(type(v) is list for v in values), values_digest=_digest(values))
    if set(schema) == {"anyOf"}:
        branches = schema["anyOf"]
        if type(branches) is not list or not 1 <= len(branches) <= 32:
            raise DomainGap("anyOf requires 1 to 32 closed branches")
        return dict(kind="union", branches=[value_domain(branch, depth + 1) for branch in branches])
    kind = schema.get("type")
    if set(schema) == {"type"} and kind in {"null", "boolean", "integer", "number", "string"}:
        return dict(kind="primitive", type=kind)
    if set(schema) == {"type", "items"} and kind == "array":
        return dict(kind="list", items=value_domain(schema["items"], depth + 1))
    if set(schema) == {"type", "additionalProperties"} and kind == "object":
        return dict(kind="string_keyed_mapping", values=value_domain(schema["additionalProperties"], depth + 1))
    if set(schema) == {"type", "properties", "required", "additionalProperties"} and kind == "object":
        properties, required, additional = schema["properties"], schema["required"], schema["additionalProperties"]
        if (type(properties) is not dict or any(type(name) is not str for name in properties)
                or type(required) is not list or any(type(name) is not str for name in required)
                or len(set(required)) != len(required) or set(required) - set(properties)):
            raise DomainGap("record fields and required names need one closed schema")
        if additional is not False and type(additional) is not dict:
            raise DomainGap("record additional values have no closed domain")
        return dict(kind="record", properties={name: value_domain(port, depth + 1) for name, port in properties.items()},
            required=list(required), additional_values="FORBIDDEN" if additional is False else value_domain(additional, depth + 1))
    # oneOf and other unimplemented constraints still lack a matching rule.
    raise DomainGap("schema shape has no matching finite native-argument rule")


def request_domain(schema, entry, node, request_codec=None):
    """Record every property, preserving UNKNOWN even when other ports close."""
    result = dict(schema="request-domain-evidence/2", rule=RULE, status="UNKNOWN", properties={},
                  keyword_domain=None, reason=None,
                  scope="host request value domain after exact group-port/3 coordinator transport only; "
                        "local NativeBindings inputs, source effects, SDK and lifecycle permission are not proved",
                  enforcement=dict(status="CHECKED" if request_codec == EXACT_REQUEST_CODEC else "UNKNOWN",
                      codec=request_codec, guards=list(TRANSPORT_GUARDS),
                      basis="Deployments._call rejects native argument references; encode_port accepts exact builtins, "
                            "finite floats and str-keyed dictionaries; HTTP JSON decode creates plain values. "
                            "A schema match or local json.dumps validation alone does not establish this domain."))
    result["signature_digest"] = hashlib.sha256(ast.dump(node.args, include_attributes=False).encode()).hexdigest()
    result["callable"] = node.name
    if (type(schema) is not dict or set(schema) != {"type", "properties", "required", "additionalProperties"}
            or schema["type"] != "object" or type(schema["properties"]) is not dict
            or type(schema["required"]) is not list or any(type(n) is not str for n in schema["properties"])
            or any(type(n) is not str for n in schema["required"])
            or len(set(schema["required"])) != len(schema["required"])
            or set(schema["required"]) - set(schema["properties"])):
        result["reason"] = "request does not have one checked object interface"
    else:
        reasons = []
        if schema["additionalProperties"] is not False:
            reasons.append("open additional keyword names have no finite domain")
        if "keyword_domain" in entry:
            domain = entry["keyword_domain"]
            try:
                if type(domain) is not dict:
                    raise DomainGap("keyword_domain is not a finite object")
                base = copy.deepcopy(schema)
                base["properties"] = {k:v for k,v in base["properties"].items() if k not in domain}
                base["additionalProperties"] = True
                rebuilt = narrow_keywords(base, node, domain, entry.get("fixed_arguments", {}))
                if rebuilt != schema or set(domain) & set(schema["required"]):
                    raise DomainGap("checked interface differs from the original finite keyword domain")
                result["keyword_domain"] = dict(status="CHECKED", names=list(domain),
                    digest=_digest(domain), all_optional=True,
                    omission="omitted names remain omitted; no defaults or arguments are injected")
            except (DomainGap, ValueError, KeyError, TypeError) as error:
                reasons.append(str(error))
                result["keyword_domain"] = dict(status="UNKNOWN", reason=str(error))
        elif node.args.kwarg is not None:
            reasons.append("original **kwargs requires an explicit finite keyword_domain")
        for name, port in schema["properties"].items():
            try:
                proof = value_domain(port)
                result["properties"][name] = dict(status="CHECKED", **proof)
            except (DomainGap, ValueError, TypeError) as error:
                result["properties"][name] = dict(status="UNKNOWN", reason=str(error))
                reasons.append(name + ": " + str(error))
        result["shape_status"] = "CHECKED" if not reasons else "UNKNOWN"
        if request_codec != EXACT_REQUEST_CODEC:
            reasons.append("exact group-port/3 request enforcement is missing; local native inputs are not covered")
        result["status"] = "CHECKED" if not reasons else "UNKNOWN"
        result["reason"] = "; ".join(reasons) if reasons else None
    result["interface_digest"] = _digest(schema)
    result["digest"] = _digest(result)
    return result
