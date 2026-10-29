"""Frozen task working sets and logical admission for a larger model pool.

Claims reserve capacity, not physical memory. Actual starts and confirmed
releases are still accounted by the deployment controller. A task holds its
whole declared working set until its owner ends, preventing hold-and-wait
between sequential calls to different model groups.
"""
from .scenarios import task_unit_ids


def freeze_working_sets(scene, tasks, placement, resources, demands, reclaimable, retention):
    groups = set(demands)
    pinned = groups - set(reclaimable)
    if retention == "fixed_resident":
        pinned = groups
    sets = {task["id"]: sorted({placement[u]["group_id"] for u in task_unit_ids(scene, task)
                              if placement[u]["kind"] == "REMOTE"}) for task in tasks}
    for identity, working in sets.items():
        required = pinned | set(working)
        for name, quota in resources.items():
            if sum(demands[g][name] for g in required) > quota:
                raise ValueError("任务工作集与不可回收常驻组超过容量：" + identity + " / " + name +
                                 "；需满足该任务原有依赖，不能通过删减必需工作放行")
    return dict(schema="task-working-sets/1", pinned=sorted(pinned), tasks=sets,
                semantics="reserve the union of admitted tasks and pinned groups until task owner ends; "
                          "pool size may exceed capacity; reservations are not physical measurements")


class WorkingSets:
    def __init__(self, spec, resources, demands):
        self.spec, self.resources, self.demands = spec, resources, demands
        self.claims = {}

    def groups(self):
        return set(self.spec["pinned"]).union(*self.claims.values())

    def admit(self, context, task_id):
        proposed = set(self.spec["tasks"][task_id])
        combined = self.groups() | proposed
        if any(sum(self.demands[g][name] for g in combined) > quota
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
