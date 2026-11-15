"""Project dispositions and semantic tool effects used by the office host."""

TEXT_ARGUMENTS = ("subject", "body", "content", "message", "text", "title", "description")
OUTBOUND_ROLES = {"privacy", "secrets", "fact_check"}


def normalize_policy(value=None):
    defaults = dict(outbound_finding="deny_operation", delivery_finding="revise_delivery", required_outbound=[],
                    privacy_entities=["CREDIT_CARD", "CRYPTO", "IBAN_CODE", "US_SSN"])
    if value is None:
        return defaults
    if not isinstance(value, dict) or set(value) - defaults.keys():
        raise ValueError("项目检查策略含未知配置")
    policy = dict(defaults, **value)
    if (policy["outbound_finding"] not in ("deny_operation", "block") or
            policy["delivery_finding"] not in ("revise_delivery", "block")):
        raise ValueError("指定处置不能作用于相应业务阶段")
    required = policy["required_outbound"]
    if (not isinstance(required, list) or any(not isinstance(role, str) or role not in OUTBOUND_ROLES
                                               for role in required) or len(required) != len(set(required))):
        raise ValueError("外发必需检查须为受支持且无重复的角色")
    entities = policy["privacy_entities"]
    if (not isinstance(entities, list) or not entities
            or any(not isinstance(item, str) or not item.strip() for item in entities)
            or len(set(entities)) != len(entities)):
        raise ValueError("项目隐私类别须为非空且不重复的名称列表")
    return dict(policy, required_outbound=sorted(required), privacy_entities=sorted(entities))


def operation(name, declared):
    if name in declared:
        return declared[name]
    return {"kind": "other"}
