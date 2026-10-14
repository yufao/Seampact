"""Finite sequential TaskPlans with actual prior-result data edges.

The result table belongs to this plan invocation. It never reconstructs a
native object from text, and it never exports an internal controller step.
These checks establish binding/control structure, not business equivalence.
"""

import json
import subprocess


def result_reference(value):
    if not isinstance(value, dict) or "$result" not in value:
        return None
    if set(value) != {"$result"}:
        raise ValueError("结果引用只能包含$result")
    reference = value["$result"]
    if not isinstance(reference, dict) or set(reference) - {"step", "path"}:
        raise ValueError("结果引用需要step及可选path")
    if not isinstance(reference.get("step"), str) or not reference["step"]:
        raise ValueError("结果引用缺少前项名称")
    path = reference.get("path", [])
    if not isinstance(path, list) or any(not isinstance(part, str) and
            not (type(part) is int and part >= 0) for part in path):
        raise ValueError("结果投影仅支持字典键和非负列表索引")
    return dict(step=reference["step"], path=path)


def _references(value, path=()):
    reference = result_reference(value)
    if reference is not None:
        yield path, reference
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _references(item, path + (name,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _references(item, path + (index,))


def _schema_at(schema, path):
    for part in path:
        if not isinstance(schema, dict):
            return {}
        if isinstance(part, str) and schema.get("type") == "object":
            schema = schema.get("properties", {}).get(part, schema.get("additionalProperties", {}))
        elif type(part) is int and schema.get("type") == "array":
            schema = schema.get("items", {})
        else:
            return {}
    return schema if isinstance(schema, dict) else {}


def _contains_native(schema):
    if not isinstance(schema, dict):
        return False
    return ("x-native-type" in schema or
        any(_contains_native(item) for item in schema.get("anyOf", [])) or
        _contains_native(schema.get("items")) or
        _contains_native(schema.get("additionalProperties")) or
        any(_contains_native(item) for item in schema.get("properties", {}).values()))


def _native_shape_matches(source, target):
    """Require a frozen original type for every native part of a data edge."""
    if "x-native-type" in target:
        expected = target["x-native-type"]
        return (isinstance(expected, dict) and
            all(expected.get(key) for key in ("source_version", "path", "symbol")) and
            source.get("x-native-type") == expected)
    if "anyOf" in target:
        return any(_native_shape_matches(source, branch) for branch in target["anyOf"]
            if _contains_native(branch))
    if target.get("type") == "array" and _contains_native(target.get("items")):
        return source.get("type") == "array" and _native_shape_matches(
            source.get("items", {}), target["items"])
    if target.get("type") == "object":
        if _contains_native(target.get("additionalProperties")):
            return source.get("type") == "object" and _native_shape_matches(
                source.get("additionalProperties", {}), target["additionalProperties"])
        members = {name: item for name, item in target.get("properties", {}).items()
            if _contains_native(item)}
        return bool(members) and source.get("type") == "object" and all(
            _native_shape_matches(source.get("properties", {}).get(name, {}), item)
            for name, item in members.items())
    return False


def _nonnative_branch_matches(source, target):
    if not source or _contains_native(source):
        return False
    if "anyOf" in target:
        return any(_nonnative_branch_matches(source, branch) for branch in target["anyOf"])
    return not _contains_native(target) and source.get("type") == target.get("type") and bool(source.get("type"))


def compile_plans(catalog, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("调用方案需要对象")
    requirements, plans = arguments.get("requirements"), arguments.get("plans")
    if not isinstance(requirements, list) or not requirements or not isinstance(plans, list) or not plans:
        raise ValueError("方案必须保留明确的任务要求和非空步骤")
    if any(not isinstance(item, str) or not item.strip() for item in requirements):
        raise ValueError("任务要求需要明确文字")
    ready = {unit["id"]: unit for unit in catalog.public() if unit["status"] == "READY"}
    completed, plan_ids = [], set()
    for proposed in plans:
        if not isinstance(proposed, dict) or not isinstance(proposed.get("id"), str) or not proposed["id"] or proposed["id"] in plan_ids:
            raise ValueError("候选方案需要唯一名称")
        plan_ids.add(proposed["id"])
        if proposed.get("requirements") != requirements:
            raise ValueError("不同候选方案不能删改本次任务要求")
        if not isinstance(proposed.get("steps"), list) or not proposed["steps"]:
            raise ValueError("候选方案缺少调用步骤")
        steps, prior, edges, controls, unit_ids = [], {}, [], [], []
        for index, proposed_step in enumerate(proposed["steps"]):
            if not isinstance(proposed_step, dict):
                raise ValueError("调用步骤需要对象")
            identity = catalog.name_to_id(proposed_step.get("unit", ""))
            unit = ready.get(identity)
            if unit is None:
                raise ValueError("方案包含未绑定的Unit")
            export = unit.get("operations", {}).get("export_entry", {})
            if export.get("status") not in {"SOURCE_BOUND", "ADMITTED"}:
                raise ValueError("内部排期片段不能作为独立入口加入组合")
            step_id = proposed_step.get("id", "step_" + str(index + 1))
            if not isinstance(step_id, str) or not step_id or step_id in prior:
                raise ValueError("方案步骤需要唯一名称")
            values = proposed_step.get("arguments")
            if not isinstance(values, dict):
                raise ValueError("Unit调用参数必须是对象")
            after = proposed_step.get("after", [])
            if not isinstance(after, list) or any(not isinstance(name, str) or name not in prior for name in after):
                raise ValueError("控制边只能连接已经位于当前步骤之前的步骤")
            dependencies = list(dict.fromkeys(after + ([steps[-1]["id"]] if steps else [])))
            deferred, native_arguments = set(), set()
            for parameter, value in values.items():
                parameter_schema = unit["input_schema"].get("properties", {}).get(parameter, {})
                for argument_path, reference in _references(value):
                    producer = prior.get(reference["step"])
                    if producer is None:
                        raise ValueError("数据边只能引用当前方案中的前项结果")
                    source_unit = ready[producer["unit_id"]]
                    source_schema = _schema_at(source_unit.get("result_schema", {}), reference["path"])
                    target_schema = _schema_at(parameter_schema, argument_path)
                    native_edge = False
                    if _contains_native(target_schema):
                        if _native_shape_matches(source_schema, target_schema):
                            native_arguments.add(parameter)
                            native_edge = True
                        elif not _nonnative_branch_matches(source_schema, target_schema):
                            raise ValueError("原生对象数据边缺少同一冻结原类型的结果端口依据")
                    if native_edge and (source_unit.get("execution") == "REMOTE" or unit.get("execution") == "REMOTE"):
                        raise ValueError("原生对象数据边不能跨进程；当前远程规则不支持迁移原引用")
                    deferred.add(parameter)
                    if reference["step"] not in dependencies:
                        dependencies.append(reference["step"])
                    edges.append(dict(source_step=reference["step"], source_unit=producer["unit_id"],
                        source_version=source_unit["source_version"], result_path=reference["path"],
                        target_step=step_id, target_unit=identity, target_version=unit["source_version"],
                        argument=parameter, argument_path=list(argument_path), exit="RETURN",
                        type_condition="SOURCE_NATIVE_TYPE" if native_edge else "CHECK_ACTUAL_VALUE"))
            catalog.validate_arguments(identity, values, deferred_arguments=deferred,
                native_arguments=native_arguments)
            step = dict(proposed_step, id=step_id, unit_id=identity, after=dependencies,
                native_arguments=sorted(native_arguments))
            for predecessor in dependencies:
                controls.append(dict(source_step=predecessor, target_step=step_id, exit="RETURN"))
            steps.append(step)
            prior[step_id] = step
            unit_ids.append(identity)
        compiled = dict(proposed, steps=steps, units=unit_ids, data_edges=edges, control_edges=controls,
            binding_status="CHECKED_WITH_RUNTIME_VALUE_CONDITIONS", semantic_status="UNRESOLVED",
            reference_scope="LOCAL_PLAN_OWNER", parallel=False,
            remote=any(ready[identity].get("execution") == "REMOTE" for identity in unit_ids),
            placements={identity: ready[identity].get("execution", "LOCAL") for identity in unit_ids})
        compiled["semantic_key"] = json.dumps(dict(
            units=[(ready[identity]["source_version"], ready[identity]["entry_id"],
                ready[identity]["path"], ready[identity]["symbol"]) for identity in unit_ids],
            data_edges=[(edge["source_step"], edge["result_path"], edge["target_step"],
                edge["argument"], edge["argument_path"]) for edge in edges]),
            ensure_ascii=False, separators=(",", ":"))
        completed.append(compiled)
    return completed


class NativePlanOwner:
    """Actual values for one sequential invocation, never an object codec."""

    def __init__(self, plan):
        self.plan = plan
        self.values, self.completed = {}, set()

    def resolve(self, value):
        reference = result_reference(value)
        if reference is not None:
            if reference["step"] not in self.completed:
                raise ValueError("前项没有成功返回，不能使能后继")
            result = self.values[reference["step"]]
            for part in reference["path"]:
                if isinstance(part, str) and type(result) is dict:
                    result = result[part]
                elif type(part) is int and type(result) in {list, tuple}:
                    result = result[part]
                else:
                    raise ValueError("实际结果不满足声明的内建容器投影，不能读取属性或触发descriptor")
            return result
        if isinstance(value, dict):
            return {name: self.resolve(item) for name, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    def execute(self, catalog, project_result):
        records, success = [], True
        needed = {edge["source_step"] for edge in self.plan["data_edges"]}
        try:
            for step in self.plan["steps"]:
                try:
                    if not set(step["after"]) <= self.completed:
                        raise ValueError("必要前项尚未成功返回，不能使能本调用")
                    arguments = self.resolve(step["arguments"])
                    value = catalog.invoke(step["unit_id"], arguments,
                        native_arguments=step["native_arguments"])
                    records.append(dict(step=step["id"], unit=step["unit_id"], result=project_result(value)))
                    if isinstance(value, subprocess.CompletedProcess) and value.returncode != 0:
                        success = False
                        break
                    self.completed.add(step["id"])
                    if step["id"] in needed:
                        self.values[step["id"]] = value
                except Exception as error:
                    success = False
                    records.append(dict(step=step["id"], unit=step["unit_id"], error=str(error)))
                    break
            return dict(plan=self.plan["id"], results=records, completed=success,
                execution_status="RETURNED" if success else "STOPPED_AT_NON_SUCCESS_EXIT",
                semantic_status="UNRESOLVED", data_edges=self.plan["data_edges"],
                control_edges=self.plan["control_edges"]), success
        finally:
            self.values.clear()
