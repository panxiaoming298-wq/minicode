"""Audited Builder replacement lineage; old patches remain immutable evidence."""
from collections.abc import Mapping


def records(events):
    return {event.payload["node_id"]: dict(event.payload["builder_recovery"])
            for event in events if event.event_type.value == "NodeReady"
            and event.payload.get("delegated") and event.payload.get("builder_recovery")}


def lineage(recoveries, task_id):
    seen = set()
    while task_id in recoveries:
        if task_id in seen:
            raise ValueError("Builder recovery lineage contains a cycle")
        seen.add(task_id)
        task_id = recoveries[task_id]["replaces_task_id"]
    return task_id, len(seen) + 1


def validate_record(record, *, node, nodes, patches, recoveries):
    if not isinstance(record, Mapping) or set(record) != {"replaces_task_id", "patch_id", "patch_sha256"}:
        raise ValueError("invalid Builder recovery identity")
    previous = record["replaces_task_id"]
    if previous not in nodes or previous == node.node_id or not previous.startswith("builder-"):
        raise ValueError("Builder recovery requires its original registered writer")
    old = nodes[previous].definition
    patch = patches.get(record["patch_id"], {})
    if (record["patch_id"] != "patch-" + previous or not patch.get("patch")
        or patch["patch"].get("patch_sha256") != record["patch_sha256"]
        or patch.get("state") in {"applying", "applied", "uncertain"}
        or patch.get("applied") or nodes[previous].state.value != "SUCCEEDED"
        or old.alternative_group is not None
        or any(getattr(node, key) != getattr(old, key) for key in
               ("objective", "role", "dependencies", "allowed_effects", "acceptance_criteria", "write_scope", "criticality", "alternative_group"))):
        raise ValueError("recovery must preserve the successful producer's assignment and unmerged patch")
    if any(value["replaces_task_id"] == previous for key, value in recoveries.items() if key != node.node_id):
        raise ValueError("this Builder already has a replacement")
    if lineage(recoveries, previous)[1] >= 3:
        raise ValueError("Builder recovery limit reached; report the unresolved blocker")
    # A consumer already pinned the old evidence. Never silently redirect it to
    # a replacement it did not observe. It needs a separately reviewed replan.
    if any(previous in item.definition.dependencies for item in nodes.values()):
        raise ValueError("Builder has registered dependency consumers; recovery requires an explicit replan")
