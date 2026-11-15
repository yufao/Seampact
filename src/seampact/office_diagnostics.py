"""Fixed normal-case admission for the November development diagnosis.

This is an experiment-only disposition override, never a project guard policy.
The fingerprints cover complete runtime cases, including their visible inputs;
client-provided risk labels and allowlists cannot authorize observation.
"""
import hashlib
import json


# Reviewed normal cases from the 2026-11-15 development selection. Only runtime
# content is fingerprinted: local source paths and private grading stay outside.
_NORMAL_CASES = {
    "development_pool:workspace:user_task_2:clean":
        (None, "33df7dcede0ad688129feb4fe84b4fae6ce2c95f76a9b1faba20ea4168bc0323"),
    "spreadsheetbench:12307":
        ("spreadsheet", "a32bcb79ca5b5215446d0c70a291ec557718bb587d89e16e368a8a4cb0bc47b2"),
    "development_pool:workspace:user_task_34:rag:clean":
        (None, "f733c80a915e21b4d1598648b64e396ad2190f04db28a3d1a18ab7731182cb87"),
    "development_pool:slack:user_task_8:clean":
        (None, "0d197fb7a1667cd0f171f291285e391602b6bc0ba331f688e38335e4ca301885"),
    "spreadsheetbench:134-14":
        ("spreadsheet", "0307353e073fd1f6c0efbb19947ccbfd665bc961ababf85ca3ff535a34458e7c"),
    "office-rag:support":
        ("office_rag", "f9df427803a21e8a81b6273ea30191f78db0dc08d04a11bdd710ee5042c835ec"),
    "development_pool:workspace:user_task_3:clean":
        (None, "3479cde1efbe764f585799840a5e2cf5570cb0ca8389e677c022d528a7114180"),
    "office-privacy:audit:normal":
        ("privacy", "20330953991a848ed7546cf1168f5bbb95f39bea51b344be56a6fa8632763684"),
    "summedits:sales_email_27_24":
        ("office_summedits", "864b5b38154d7e47d6ae58024fa5f25768eab2a3f12508324b97e3c153f68b55"),
    "office-routing:public-event-publication:dev":
        ("office_routing", "0b2414e89faba168d334fa264b059cd3e1de4fa951c5594ad1c52bbbc0629fd5"),
    "office-access:9fb8002671a6":
        ("office_access", "b0f97c439c0903c8069ec7c642df771f6d5040da36ec8ca0d31b6f167ed5ddc4"),
    "office-links:6c16701cad23c41e01da":
        ("office_links", "0d4cff34b60211cbb4d5334715ab69abeb5e525222ab70fa201ce00879c40011"),
}


class _ObservationPermit:
    """In-memory authorization, created only after frozen-case validation."""


def diagnostic_mode(plan):
    mode = plan.get("diagnostic_checks", "enforce")
    if mode not in ("enforce", "observe_specified"):
        raise ValueError("诊断检查模式须为 enforce 或 observe_specified")
    return mode


def _check_normal_case(case):
    expected = _NORMAL_CASES.get(case.get("id"))
    runtime = {key: value for key, value in case.items() if key not in {"_source", "_manifest", "_grading"}}
    digest = hashlib.sha256(json.dumps(runtime, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    if (case.get("split") != "development_pool" or case.get("injection_task_id") or case.get("injections")
            or expected != (case.get("_source"), digest)):
        raise ValueError("观察检查只允许服务器白名单中内容未变的正常开发题")


def _check_scope(plan):
    if plan.get("local_development_only") is not True:
        raise ValueError("观察检查必须显式限定 local_development_only")
    if plan.get("development_selector", "app_config") != "app_config":
        raise ValueError("观察检查须使用 app_config 保留全部适用检查")


def validate_diagnostic_plan(plan, cases):
    if diagnostic_mode(plan) == "enforce":
        return
    _check_scope(plan)
    for identity in plan["cases"]["development"]:
        _check_normal_case(cases[identity])


def observation_permit(plan, mode, case):
    """Revalidate at admission; the permission is not serialized or prompted."""
    if diagnostic_mode(plan) == "enforce":
        return None
    if mode != "development" or case["id"] not in plan["cases"]["development"]:
        raise ValueError("观察检查只允许当前协议的开发题派发")
    _check_scope(plan)
    _check_normal_case(case)
    return _ObservationPermit()


def observation_enabled(config):
    permit = config.get("_diagnostic_observation")
    if permit is None:
        if diagnostic_mode(config) != "enforce":
            raise ValueError("观察检查须由正常开发题入口授权")
        return False
    if (not isinstance(permit, _ObservationPermit) or config.get("mode") != "app_config"
            or diagnostic_mode(config) != "observe_specified"):
        raise ValueError("观察检查须由正常开发题入口授权")
    return True
