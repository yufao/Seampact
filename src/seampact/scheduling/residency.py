"""Frozen task working sets and logical admission for a larger model pool.

Claims reserve capacity, not physical memory. Actual starts and confirmed
releases are still accounted by the deployment controller. A task holds its
whole declared working set until its owner ends, preventing hold-and-wait
between sequential calls to different model groups.
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
                        service_dependencies=None, shared_services=None):
    groups = set(demands)
    pinned = groups - set(reclaimable)
    if retention == "fixed_resident":
        pinned = groups
    dependencies = service_dependencies or {}
    pinned = dependency_closure(pinned, dependencies)
    sets = {}
    for task in tasks:
        required = set(shared_services or [])
        for unit in task_unit_ids(scene, task):
            target = placement[unit]
            if target["kind"] == "REMOTE":
                required.add(target["group_id"])
            required.update(target.get("resource_dependencies", []))
        required = dependency_closure(required, dependencies)
        if resources and required - groups:
            raise ValueError("任务依赖没有完整资源需求：" + ", ".join(sorted(required - groups)))
        sets[task["id"]] = sorted(required)
    for identity, working in sets.items():
        required = pinned | set(working)
        for name, quota in resources.items():
            if sum(demands[g][name] for g in required) > quota:
                raise ValueError("任务工作集与不可回收常驻组超过容量：" + identity + " / " + name +
                                 "；需满足该任务原有依赖，不能通过删减必需工作放行")
    return dict(schema="task-working-sets/2", pinned=sorted(pinned), tasks=sets,
                dependencies={g: sorted(v) for g, v in dependencies.items()},
                semantics="reserve the union of admitted tasks and pinned groups until task owner ends; "
                          "pool size may exceed capacity; reservations are not physical measurements")


class WorkingSets:
    def __init__(self, spec, resources, demands):
        self.spec, self.resources, self.demands = spec, resources, demands
        self.claims = {}

    def groups(self):
        return set(self.spec["pinned"]).union(*self.claims.values())

    def admit(self, context, task_id, occupancy=None):
        proposed = set(self.spec["tasks"][task_id])
        combined = self.groups() | proposed
        actual = occupancy or {}
        if any(sum(max(actual.get(g, 0), int(g in combined)) * self.demands[g][name]
                   for g in set(actual) | combined) > quota
               for name, quota in self.resources.items()):
            return False
        self.claims[context] = proposed
        return True

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
