"""Explain existing source checks without granting execution or new operations."""

import copy

from .classification import classify_target


TASK_ROLES = {"target_defense", "evidence", "host", "support", "display", "mock", "offline", "unknown"}
TASK_ROLE_RULES = """
entry和target可提供task_role:{role:"target_defense/evidence/host/support/display/mock/offline/unknown",purpose:"对当前目标的实际用途",evidence:[{path,unit,start,end}]}。
分别表示目标防护、补证、宿主、必要支撑、展示、测试替身、离线和未知。依据真实调用和活动配置填写；不能按文件名排除未知目标。
task_role始终是分析声明；检查器只核验冻结证据范围及原定义锚点，不证明用途或防护效果，不改变入口准入。
check工具的feedback将具体field_paths、issue_code、repair_action分为model_repairs和external_conditions。按字段修正候选/补证；规则缺口、资源未选、运行环境或初始化未运行应如实保留，不要求全部READY。
submit结束本次分析，outcome单独表示仍需修正、等待外部条件或静态核对且未运行；COMPLETED不表示原能力已跑通。不得为修复资源条件自行调用真实服务、下载或购买资源。
"""


def task_role_claim(inspector, target):
    """Check only claim shape and frozen references, never infer semantic roles."""
    claim = target.get("task_role")
    result = dict(role="unknown", purpose=None, evidence=[], status="NOT_DECLARED",
                  semantic_claims="NOT_INDEPENDENTLY_PROVED", admission_granted=False)
    if claim is None:
        return result
    try:
        if not isinstance(claim, dict) or claim.get("role") not in TASK_ROLES:
            raise ValueError("task_role requires a declared role")
        result.update(role=claim["role"], purpose=claim.get("purpose"))
        if not isinstance(claim.get("purpose"), str) or not claim["purpose"].strip():
            raise ValueError("task_role requires an actual purpose")
        if not isinstance(claim.get("evidence"), list) or not claim["evidence"]:
            raise ValueError("task_role needs frozen source evidence")
        # Shared source reference checks are imported lazily to avoid the
        # preprocessing/report module cycle. No original module is imported.
        from .preprocessing import _anchor, _evidence
        cache = {}
        anchor = _anchor(inspector, target, cache)
        result["evidence"] = [_evidence(inspector, ref, cache) for ref in claim["evidence"]]
        if not any(ref["path"] == anchor["path"] and ref["unit"] == anchor["unit"]
                   and (anchor["status"] == "FILE_REFERENCED" or ref["start"] <= anchor["line"] <= ref["end"])
                   for ref in result["evidence"]):
            raise ValueError("task_role evidence must cover its original definition or CLI file")
        result.update(status="SOURCE_REFERENCED", anchor=anchor)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, UnicodeError) as error:
        result.update(status="MISSING", reason=str(error))
    return result


def _references_dependency(value, identity):
    if isinstance(value, dict):
        ref = value.get("$dependency")
        return (isinstance(ref, dict) and ref.get("id") == identity
                or any(_references_dependency(child, identity) for child in value.values()))
    return isinstance(value, list) and any(_references_dependency(child, identity) for child in value)


def _typed_issue(category, title, reason, action, target, *, issue_code=None,
                 repair_scope=None, model_repairable=None, field_paths=None, **evidence):
    scopes = {"parameter_mapping": "candidate", "resource_binding": "resource",
              "source_anchor": "evidence", "unsupported_rule": "rule",
              "runtime_environment": "environment", "binding_unresolved": "initialization"}
    scope = repair_scope or scopes.get(category, "candidate")
    return dict(category=category, title=title, reason=reason, action=action, target=target,
                issue_code=issue_code or category.upper(), repair_action=action, repair_scope=scope,
                model_repairable=(scope in {"candidate", "evidence"} if model_repairable is None else model_repairable),
                field_paths=field_paths or [], **evidence)


def target_classification(inspector, targets):
    profiles, seen = [], set()
    for target in targets:
        if not isinstance(target, dict):
            continue
        key = (str(target.get("path", "")), str(target.get("unit", "file")),
               str(target.get("symbol", "")), str(target.get("kind", "function")))
        if key in seen:
            continue
        seen.add(key)
        profile = classify_target(inspector, target)
        profile["task_role"] = task_role_claim(inspector, target)
        profiles.append(profile)
    return dict(schema="harness-classification/1", source_version=inspector.request["source_version"],
                profiles=profiles, total=len(profiles), next_offset=None,
                scope="仅提交的目标范围；不表示已完成整个仓库的发现或语义证明",
                execution="NOT_RUN", admission_granted=False)


def entry_readiness(inspector, candidate, report):
    """Preserve structured check provenance; unknown reasons stay unresolved."""
    bindings = {row.get("id"): row for row in report.get("entry_bindings", {}).get("entries", [])}
    boundaries = {row.get("id"): row for row in report.get("boundaries", [])}
    dependencies = report.get("dependencies", [])
    selections = inspector.request.get("dependency_bindings") or {}
    result = []
    for index, entry in enumerate(candidate.get("entries", [])):
        if not isinstance(entry, dict):
            continue
        identity = entry.get("id")
        checked = bindings.get(identity, {})
        issues = []
        entry_path = "entries[" + str(index) + "]"

        def issue(category, title, reason, action, target, **evidence):
            evidence.setdefault("field_paths", [entry_path])
            issues.append(_typed_issue(category, title, reason, action, target, **evidence))

        for field in ("id", "path", "symbol", "kind"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                issue("candidate_incomplete", "入口候选字段不完整", "缺少入口字段：" + field,
                      "从冻结源码补齐该入口字段后完整重检。", "revision",
                      issue_code="candidate_incomplete", repair_scope="candidate",
                      field_paths=[entry_path + "." + field])
        claim = task_role_claim(inspector, entry)
        if claim["status"] == "MISSING":
            issue("evidence_incomplete", "入口用途声明的证据尚未核对", claim["reason"],
                  "补齐用途字段及覆盖原定义的冻结源码证据；不能用分类声明代替绑定证明。", "revision",
                  issue_code="evidence_incomplete", repair_scope="evidence",
                  field_paths=[entry_path + ".task_role"])

        for dep_index, dep in enumerate(dependencies):
            referenced = _references_dependency(entry, dep.get("id"))
            if not referenced and (identity not in dep.get("entry_ids", []) or dep.get("required", True) is False):
                continue
            mapping = dep.get("entry_checks", {}).get(identity, {})
            dep_id = dep.get("id")
            if mapping.get("status") != "MAPPING_CHECKED":
                details = mapping.get("issues") or [mapping or dep]
                for detail in details:
                    code = detail.get("issue_code") or "parameter_mapping"
                    scope = {"candidate_incomplete": "candidate", "evidence_incomplete": "evidence",
                             "parameter_flow_unsupported": "rule", "resource_locator_unsupported": "rule"}.get(code, "candidate")
                    dep_path = "dependencies[" + str(dep_index) + "]"
                    paths = [dep_path if path == "dependencies" else path
                             if path.startswith(("dependencies[", "entries", "dependency_bindings"))
                             else dep_path + "." + path for path in detail.get("field_paths", [])]
                    issue("parameter_mapping", "原参数与资源的映射尚未确定",
                          detail.get("reason") or mapping.get("reason") or dep.get("reason") or "尚无该入口的已核对映射",
                          detail.get("repair_action") or "补查原构造或调用位置，修正映射后完整重检；仅选择资源不能补齐映射。",
                          "revision", dependency_id=dep_id, issue_code=code, repair_scope=scope,
                          field_paths=paths or [dep_path, entry_path],
                          source_evidence=copy.deepcopy(detail.get("evidence", [])))
            elif dep.get("configurable"):
                selected = selections.get(dep_id) or {}
                if not selected or selected.get("definition_digest") != dep.get("definition_digest"):
                    issue("resource_binding", "需要选择与原映射匹配的资源",
                          "尚未选择资源，或选择所对应的原映射已变化。",
                          "登记并检查资源，在该依赖下选择绑定；保存后零模型重检。",
                          "weights" if dep.get("kind") == "weight" else "services", dependency_id=dep_id,
                          issue_code="resource_not_selected", field_paths=["dependency_bindings." + str(dep_id)])

        for boundary_id in entry.get("boundary_ids", []):
            row = boundaries.get(boundary_id)
            if not row:
                issue("candidate_incomplete", "入口引用的边界未提供", "缺少边界：" + str(boundary_id),
                      "补齐实际原边界或修正该引用后完整重检。", "revision",
                      issue_code="candidate_incomplete", repair_scope="candidate",
                      field_paths=[entry_path + ".boundary_ids", "boundaries"])
                continue
            obligations = row.get("obligations", {})
            anchor = obligations.get("source_anchor", {})
            rule = obligations.get("binding_rule", {})
            if anchor.get("status") == "FAIL":
                issue("source_anchor", "拆分位置未匹配到原源码",
                      "边界引用的定义不存在或不唯一。",
                      "核对冻结版本、文件和符号，在修订台修正候选后重检。", "revision", boundary_id=boundary_id)
            elif rule.get("status") == "UNSUPPORTED":
                issue("unsupported_rule", "当前源码形式不符合拆分规则",
                      rule.get("reason") or "所选变换不受现有规则支持。",
                      "查看具体规则缺口，核对能否保留更大的原入口；确需新规则时修通用实现。资源绑定不能解除此条件。",
                      "revision", boundary_id=boundary_id)

        for gap in (checked.get("host_condition") or {}).get("gaps", []):
            # These are typed environment deferrals from the native checker,
            # not guesses made by matching words in an exception message.
            issue("runtime_environment", "执行环境或资源条件尚未满足", gap,
                  "核对目标解释器、依赖及 worker 可见的资源和凭据，补齐后零模型重检。",
                  "environment")
        if checked.get("code") == "initialization_incomplete":
            for gap in checked.get("initialization_gaps") or [checked]:
                issue("initialization_incomplete", "原对象的构造后初始化尚未保留", gap.get("reason") or checked.get("reason"),
                      "追踪所列原构造与初始化操作，使用规则已支持的完整原 B-CALL 前缀；不省略加载、设备或模式设置。覆盖不了则保留规则缺口。",
                      "revision", issue_code="initialization_incomplete", repair_scope="candidate",
                      field_paths=[entry_path + ".binding_program", entry_path + ".initializers"],
                      source_evidence=copy.deepcopy(gap))
        if checked.get("status") != "READY" and not issues:
            issue("binding_unresolved", "入口绑定仍需核对",
                  checked.get("reason") or report.get("entry_bindings", {}).get("reason") or "尚未取得入口绑定检查结果。",
                  checked.get("repair_action") or "按原始原因补查签名、对象构造或规则条件后重检；无法确定原因时保留待核对，不猜成缺资源。",
                  "revision", issue_code=checked.get("issue_code") or "initialization_unverified",
                  field_paths=checked.get("field_paths") or [entry_path])
        # Retain the complete checker reason even when an earlier prerequisite
        # has already yielded an actionable issue; never claim it is exhaustive.
        result.append(dict(entry_id=identity, path=entry.get("path"), symbol=entry.get("symbol"),
            status="STATIC_READY" if checked.get("status") == "READY" and not issues else "NEEDS_REPAIR",
            binding_status=checked.get("status", "NOT_CHECKED"), reason=checked.get("reason"),
            initialization="NOT_RUN", execution="NOT_RUN",
            parameters=copy.deepcopy(checked.get("parameters")),
            operations=dict(entry=copy.deepcopy(checked.get("operations", {})),
                boundaries={name: copy.deepcopy(boundaries[name].get("operations", {}))
                            for name in entry.get("boundary_ids", []) if name in boundaries}),
            task_role=claim, issues=issues,
            scope="仅本次静态检查；实际初始化与调用另由运行记录保存",
            next_step=("按真实参数启动并调用目标入口，检查原结果；部署后在目标 worker 再核验。"
                       if checked.get("status") == "READY" and not issues else
                       "先处理列出的缺口并重新核对；后续运行仍可能暴露其他条件。")))
    return result


def candidate_feedback(candidate, report):
    """Typed repair work and external deferrals; no error-string heuristics."""
    issues = [dict(issue, entry_id=row.get("entry_id"))
              for row in report.get("entry_readiness", []) for issue in row.get("issues", [])]
    reported_boundaries = {issue.get("boundary_id") for issue in issues}
    for index, boundary in enumerate(report.get("boundaries", [])):
        if boundary.get("id") in reported_boundaries:
            continue
        obligations = boundary.get("obligations") or {}
        if (obligations.get("source_anchor") or {}).get("status") == "FAIL":
            issues.append(_typed_issue("source_anchor", "边界源码锚点未核对", "原定义不存在或不唯一。",
                "核对冻结源码的文件、符号及单元，修正边界后重检。", "revision",
                field_paths=["boundaries[" + str(index) + "]"], boundary_id=boundary.get("id")))
        elif (obligations.get("binding_rule") or {}).get("status") == "UNSUPPORTED":
            rule = obligations["binding_rule"]
            issues.append(_typed_issue("unsupported_rule", "边界形式尚无规则支持", rule.get("reason"),
                "核对较大的原入口是否可保留，不能靠资源绑定解除规则缺口。", "revision",
                field_paths=["boundaries[" + str(index) + "]"], boundary_id=boundary.get("id")))
    for issue in report.get("issues", []):
        # Preprocessing reference failures are explicitly scoped by its checker.
        field = issue.get("field")
        if field in {"targets", "entries"}:
            path = field + ("[" + str(issue["index"]) + "]" if "index" in issue else "")
            issues.append(_typed_issue("evidence_incomplete", "发现候选或源码证据不完整", issue.get("reason"),
                "补查冻结源码并修正此字段；未发现真实入口时保留具体缺口。", "revision",
                issue_code="evidence_incomplete" if "index" in issue else "candidate_incomplete",
                repair_scope="evidence" if "index" in issue else "candidate", field_paths=[path]))
    if not candidate.get("entries") and not any(issue.get("field_paths") == ["entries"] for issue in issues):
        issues.append(_typed_issue("candidate_incomplete", "尚无真实入口候选", "本次候选未提供可核对入口。",
            "继续追踪原目标；若无法发现则保留具体缺口，不用辅助函数替代。", "revision",
            issue_code="candidate_incomplete", repair_scope="candidate", field_paths=["entries"]))
    model = [issue for issue in issues if issue["model_repairable"]]
    external = [issue for issue in issues if not issue["model_repairable"]]
    outcome = "NEEDS_CANDIDATE_REPAIR" if model else "AWAITING_EXTERNAL_CONDITIONS" if external else "STATIC_CHECKED_NOT_RUN"
    return dict(schema="intake-feedback/1", model_repairs=model, external_conditions=external,
                outcome=outcome, execution="NOT_RUN", initialization="NOT_RUN",
                pending_execution=[dict(issue_code="initialization_not_run", repair_scope="initialization",
                    repair_action="在明确授权的运行环节核对原初始化与真实输入输出；静态核对不证明兼容或效果。",
                    model_repairable=False, field_paths=["entries"], blocking_static_check=False)],
                retry="EXPLICIT_MODEL_TOOL_LOOP_ONLY", admission_granted=False)


def explain_candidate(inspector, candidate, report):
    report["classification"] = target_classification(inspector, candidate.get("entries", []))
    report["entry_readiness"] = entry_readiness(inspector, candidate, report)
    report["feedback"] = candidate_feedback(candidate, report)
    report["outcome"] = report["feedback"]["outcome"]
    return report
