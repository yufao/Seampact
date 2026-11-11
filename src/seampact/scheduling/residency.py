"""Selected-call reservations, retained owners, and actual held allocations.

The allowed capability pool is not a task obligation. A selected business
region reserves all of its calls together; absent checked stage retirement,
that reservation remains until its business owner exits. Physical instances
continue to count after a logical claim ends, until release is confirmed.
"""
from seampact.scenes.scenarios import task_unit_ids


def dependency_closure(allocations, dependencies):
    result, pending = set(), list(allocations)
    while pending:
        identity = pending.pop()
        if identity not in result:
            result.add(identity)
            pending.extend(dependencies.get(identity, []))
    return result


def freeze_working_sets(scene, tasks, placement, resources, demands, reclaimable, retention,
                        service_dependencies=None, shared_services=None, offline_allocations=(),
                        initial_allocations=()):
    groups = set(demands)
    business_groups = groups - set(offline_allocations)
    # Lifecycle UNKNOWN prevents retiring an allocation that exists; it does
    # not create a cold allocation or turn the whole optional pool into P0.
    initial = business_groups & set(initial_allocations)
    pinned = initial - set(reclaimable)
    if retention == "fixed_resident":
        pinned = initial
    dependencies = service_dependencies or {}
    pinned = dependency_closure(pinned, dependencies)
    calls = {}
    allowed = {task["id"]: task_unit_ids(scene, task) for task in tasks}
    for unit in set().union(*(set(units) for units in allowed.values())):
        target = placement[unit]
        required = set(target.get("resource_dependencies", []))
        if target["kind"] == "REMOTE":
            required.add(target["group_id"])
        calls[unit] = sorted(dependency_closure(required, dependencies))
    base = dependency_closure(shared_services or [], dependencies)
    for identity, required in {"pinned": pinned, "business roles": base, **calls}.items():
        if resources and set(required) - groups:
            raise ValueError("调用依赖没有完整资源需求：" + identity + " / " +
                             ", ".join(sorted(set(required) - groups)))
    # The union of optional capabilities is not a committed working set. An
    # impossible individual call stays in the pool but cannot be selected.
    for name, quota in resources.items():
        if sum(demands[g][name] for g in pinned | base) > quota:
            raise ValueError("业务工作集与不可回收常驻组超过容量：" + name)
    return dict(schema="task-working-sets/3", pinned=sorted(pinned), initial_allocations=sorted(initial),
                tasks={identity: sorted(base) for identity in allowed}, calls=calls, allowed=allowed,
                dependencies={g: sorted(v) for g, v in dependencies.items()},
                semantics="unselected capabilities create no dynamic claim; atomically reserve the full "
                          "selected business region plus pinned and retained owners; selected closures are "
                          "conservatively retained until business task exit, not returned at each root return; "
                          "held instances and unresolved starts remain charged until confirmed release")


class WorkingSets:
    def __init__(self, spec, resources, demands):
        self.spec, self.resources, self.demands = spec, resources, demands
        self.claims = {}

    def groups(self):
        return set(self.spec["pinned"]).union(*self.claims.values())

    def proposed(self, context, task_id, units=()):
        if set(units) - set(self.spec["allowed"][task_id]):
            raise ValueError("调用超出当前任务冻结能力范围")
        result = set(self.claims.get(context, ())) | set(self.spec["tasks"][task_id])
        for unit in units:
            result.update(self.spec["calls"][unit])
        return result

    def fits(self, context, task_id, occupancy=None, units=(), include_other=True):
        proposed = self.proposed(context, task_id, units)
        combined = (self.groups() if include_other else set(self.spec["pinned"])) | proposed
        actual = occupancy or {}
        return not any(sum(max(actual.get(g, 0), int(g in combined)) * self.demands[g][name]
                           for g in set(actual) | combined) > quota
                       for name, quota in self.resources.items())

    def admit(self, context, task_id, occupancy=None, units=()):
        if not self.fits(context, task_id, occupancy, units):
            return False
        self.claims[context] = self.proposed(context, task_id, units)
        return True

    def covers(self, context, task_id, units):
        return self.proposed(context, task_id, units) <= self.claims.get(context, set())

    def release(self, context):
        self.claims.pop(context, None)

    def used(self, occupancy):
        claimed = self.groups()
        return {name: sum(max(occupancy.get(g, 0), int(g in claimed)) * self.demands[g][name]
                          for g in set(occupancy) | claimed) for name in self.resources}

    def after_start(self, group, occupancy):
        after = dict(occupancy)
        after[group] = after.get(group, 0) + 1
        used = self.used(after)
        return min(((quota - used[name]) / quota for name, quota in self.resources.items()), default=None)
