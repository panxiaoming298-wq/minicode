"""Versioned collaboration policy and bounded, non-authoritative task metadata."""
from collections.abc import Mapping

POLICY = "continuous-agent-collaboration-v1"
LIMITS = {"max_generations": (16, 128), "max_sessions": (8, 64),
          "max_tasks": (64, 512), "max_messages": (1024, 16384), "max_pending_messages": (32, 128)}
RUNTIME_KEYS = {"max_generations": "runtimeAgentMaxGenerations", "max_sessions": "runtimeAgentMaxSessions",
                "max_tasks": "runtimeAgentMaxTasks", "max_messages": "runtimeAgentMaxMessages",
                "max_pending_messages": "runtimeAgentMaxPendingMessages"}


def resolve_policy(runtime, *, enabled, max_agents):
    selected = runtime.get("runtimeAgentCollaboration", enabled)
    if type(selected) is not bool or (selected and not enabled):
        raise ValueError("runtimeAgentCollaboration requires checkpointed async agent messages")
    if not selected:
        return None
    config = {"policy": POLICY, "peer_messages": runtime.get("runtimeAgentPeerMessages", True)}
    for key, (default, ceiling) in LIMITS.items():
        config[key] = runtime.get(RUNTIME_KEYS[key], max(default, max_agents) if key == "max_tasks" else default)
    validate_config(config)
    return config


def validate_config(config):
    if (not isinstance(config, Mapping) or set(config) != {"policy", "peer_messages", *LIMITS}
            or config["policy"] != POLICY or type(config["peer_messages"]) is not bool):
        raise ValueError("invalid continuous collaboration policy")
    for key, (_, ceiling) in LIMITS.items():
        if type(config[key]) is not int or not 1 <= config[key] <= ceiling:
            raise ValueError(f"{RUNTIME_KEYS[key]} must be an integer between 1 and {ceiling}")


def assignment_metadata(spec):
    return {"name": spec.name, "checks": [dict(check) for check in spec.checks],
            **({"optional": True} if spec.optional else {})}


def result_notice(record, result, settlement):
    """A native result locator with a bounded excerpt, never an acceptance claim."""
    summary = result.output_summary if result else "Activation ended without an accepted task result; inspect its state, checkpoint and retained files before continuing."
    refs = [ref.to_dict() for ref in result.artifact_refs] if result else settlement.get("artifact_refs", [])
    return {"task_id": record["task_id"], "session_id": record["session_id"],
            "name": record.get("name", ""), "generation": record["generation"],
            "status": result.status.value if result else settlement["result_status"], "attempt_id": settlement["attempt_id"],
            "summary": summary[:1200], "summary_truncated": len(summary) > 1200,
            "artifact_refs": refs[:8], "artifact_count": len(refs), "input_revision": record["input_revision"],
            "write_scope": record["definition"].get("write_scope", []),
            "worker_exited": settlement is not None,
            "ambiguous_side_effect": bool((settlement or {}).get("ambiguous_side_effect")),
            "counts_as_acceptance": False}
