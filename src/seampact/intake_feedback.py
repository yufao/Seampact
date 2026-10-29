"""Explain existing source checks without granting execution or new operations."""

import copy

from .classification import classify_target


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
    for entry in candidate.get("entries", []):
        if not isinstance(entry, dict):
            continue
        identity = entry.get("id")
        checked = bindings.get(identity, {})
        issues = []

        def issue(category, title, reason, action, target, **evidence):
            issues.append(dict(category=category, title=title, reason=reason,
                               action=action, target=target, **evidence))

        for dep in dependencies:
            if identity not in dep.get("entry_ids", []) or dep.get("required", True) is False:
                continue
            mapping = dep.get("entry_checks", {}).get(identity, {})
            dep_id = dep.get("id")
            if mapping.get("status") != "MAPPING_CHECKED":
                issue("parameter_mapping", "原参数与资源的映射尚未确定",
                      mapping.get("reason") or dep.get("reason") or "尚无该入口的已核对映射",
                      "在修订台补查原构造或调用位置，修正映射后完整重检；仅选择资源不能补齐映射。",
                      "revision", dependency_id=dep_id)
            elif dep.get("configurable"):
                selected = selections.get(dep_id) or {}
                if not selected or selected.get("definition_digest") != dep.get("definition_digest"):
                    issue("resource_binding", "需要选择与原映射匹配的资源",
                          "尚未选择资源，或选择所对应的原映射已变化。",
                          "登记并检查资源，在该依赖下选择绑定；保存后零模型重检。",
                          "weights" if dep.get("kind") == "weight" else "services", dependency_id=dep_id)

        for boundary_id in entry.get("boundary_ids", []):
            row = boundaries.get(boundary_id)
            if not row:
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
        if checked.get("status") != "READY" and not issues:
            issue("binding_unresolved", "入口绑定仍需核对",
                  checked.get("reason") or report.get("entry_bindings", {}).get("reason") or "尚未取得入口绑定检查结果。",
                  "按下方原始原因补查签名、对象构造或规则条件，再在修订台重检；尚不能归因为缺权重。",
                  "revision")
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
            issues=issues,
            scope="仅本次静态检查；实际初始化与调用另由运行记录保存",
            next_step=("按真实参数启动并调用目标入口，检查原结果；部署后在目标 worker 再核验。"
                       if checked.get("status") == "READY" and not issues else
                       "先处理列出的缺口并重新核对；后续运行仍可能暴露其他条件。")))
    return result


def explain_candidate(inspector, candidate, report):
    report["classification"] = target_classification(inspector, candidate.get("entries", []))
    report["entry_readiness"] = entry_readiness(inspector, candidate, report)
    return report
