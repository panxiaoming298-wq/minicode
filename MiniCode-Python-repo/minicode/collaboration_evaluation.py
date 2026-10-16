"""Comparability receipts and bounded task registration for Web Ultra trials."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ARMS = ("primary_only", "autonomous_readonly", "autonomous_parallel_candidates")
# The historical pair. A plan recorded before the parallel arm existed must keep
# its original meaning, so it is interpreted with this tuple, never with ARMS.
LEGACY_ARMS = ("primary_only", "autonomous_readonly")
PARALLEL_ARM = "autonomous_parallel_candidates"
SEQUENTIAL_BUILDER_ARM = "sequential_builder_candidates"
BUILDER_ARMS = frozenset({PARALLEL_ARM, SEQUENTIAL_BUILDER_ARM})
# Opt-in control; existing default/recorded experiments retain their three arms.
COMPARISON_ARMS = (*ARMS, SEQUENTIAL_BUILDER_ARM)
SCHEMA = "minicode-web-collaboration-v1"
# The protocol version an assembly receipt was produced under. Raising this is
# what lets a reader tell an old receipt from one that carries the parallel
# candidate fields.
RECEIPT_PROTOCOL_VERSION = "primary-policy-v1"
DELEGATION_TOOLS = frozenset({"task", "consult_agents"})
# The native parallel-candidate tools. A receipt that claims the parallel arm must
# show these really registered for the primary.
BUILDER_TOOL_NAMES = ("spawn_builders", "poll_task_results", "wait_dependencies",
                      "accept_builder_patch", "cancel_task", "select_builder_candidate")
# Only the primary arm is allowed to have no delegation tooling; both
# autonomous arms must really be able to delegate for the comparison to mean
# anything.
AUTONOMOUS_ARMS = frozenset({"autonomous_readonly", *BUILDER_ARMS})
# The experimental TREATMENT. An arm exists to change exactly these, so they must
# not be compared as if they were drift: each arm's values are checked against its
# own declared capacity instead, and they stay visible in the receipt's
# ``effective_concurrency`` block. Everything else -- model route, output policy,
# total budget, memory, non-delegation tools and permissions, task input -- stays
# in the compared set.
ARM_TREATMENT_KEYS = ("runtimeParallelCandidates", "runtimeMaxActiveWriters",
                      "runtimeMaxActiveWorkers")
POLICY_TREATMENT_KEYS = ("max_active_workers", "max_active_writers",
                         "parallel_candidates", "effective_writer_capacity")


def arm_runtime_overrides(arm: str) -> dict[str, Any]:
    """The runtime settings that make an arm real.

    These are the only difference the Runtime needs: the arms share the model,
    the tools, the memory and the non-delegation permission set. An arm name in
    a report that does not carry these settings is a relabelled run, not a new
    mode, which is why the receipt records the effective values it actually got.
    """
    if arm not in COMPARISON_ARMS:
        raise ValueError("unknown comparison arm")
    if arm == PARALLEL_ARM:
        return {"runtimeParallelCandidates": True, "runtimeMaxActiveWriters": 3,
                "runtimeMaxActiveWorkers": 3}
    if arm == SEQUENTIAL_BUILDER_ARM:
        return {"runtimeParallelCandidates": True, "runtimeMaxActiveWriters": 1,
                "runtimeMaxActiveWorkers": 1}
    # Serial arms must remain serial when the product's Ultra default changes.
    return {"runtimeParallelCandidates": False, "runtimeMaxActiveWriters": 1}


def arm_capacity_expectation(arm: str) -> dict[str, Any]:
    """What concurrency the arm claims, so a report cannot imply more."""
    overrides = arm_runtime_overrides(arm)
    return {"parallel_candidates": overrides.get("runtimeParallelCandidates") is True,
            "max_active_writers": overrides.get("runtimeMaxActiveWriters", 1),
            "max_active_workers": overrides.get("runtimeMaxActiveWorkers", 1),
            "target": "primary plus up to two Builders" if arm in BUILDER_ARMS else "single writer"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# The registered per-run ceilings for a collaboration trial. The token ceiling is
# what one run may spend across EVERY model call it makes, including the primary's
# own multi-step loop and any auxiliary route; a registered task that needs more
# than this is reported as not fitting rather than silently given a bigger budget.
MAX_RUN_TOKENS = 648_000
MAX_RUN_SECONDS = 600

# The largest single-response output allowance a collaboration trial may set.
# The review of the 128k batch found the real failure: a request whose output cap
# was the adapter's 4096 fallback spent every one of those tokens on reasoning and
# returned no tool call and no answer, terminating as ModelOutputTruncated. Raising
# the RUN budget cannot address that, because the run budget was never the limit
# that fired. The per-request allowance is therefore set explicitly and bounded.
MAX_REQUEST_OUTPUT_TOKENS = 16_384

# What one request gets by default, as a fraction of the run ceiling. This is the
# second half of the same finding: an allowance that is generous relative to the RUN
# ceiling is a reservation that stops later requests from being admitted at all. The
# batching default of an eighth left a 40k run unable to fund its second request
# (reserving 5k output on top of a ~20k input estimate against a 36k run scope).
# A sixteenth funds several turns and still covers the observed reasoning need.
_REQUEST_OUTPUT_RUN_FRACTION = 16
# One request's share is never smaller than this, so a tiny run still allows a
# reasoning-sized answer rather than an unusable one.
_MIN_REQUEST_OUTPUT_TOKENS = 1024
# ...and never larger than this, whatever the run ceiling is. Kept well under the
# experiment ceiling so no single response can consume a whole small run.
_MAX_DEFAULT_REQUEST_OUTPUT_TOKENS = 8_192


def request_output_limit(max_tokens: int, requested: int | None = None) -> int:
    """The explicit per-request output allowance for one bounded trial run.

    The allowance must be able to hold the reasoning a request actually spends
    before it can act, and it must stay small enough relative to the RUN ceiling that
    the run can still fund its later requests. Both halves are enforced: a value above
    the ceiling is refused rather than silently clamped, and the default is derived
    from the run ceiling instead of being a constant.
    """

    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("a run token ceiling is required to bound a single response")
    ceiling = min(MAX_REQUEST_OUTPUT_TOKENS, max_tokens)
    if requested is None:
        floor = min(_MIN_REQUEST_OUTPUT_TOKENS, ceiling)
        share = min(ceiling, -(-max_tokens // _REQUEST_OUTPUT_RUN_FRACTION))
        return min(_MAX_DEFAULT_REQUEST_OUTPUT_TOKENS, max(floor, share))
    if type(requested) is not int or requested < 1:
        raise ValueError("a requested output limit must be a positive integer")
    if requested > ceiling:
        raise ValueError(
            f"a single response may not exceed {ceiling} output tokens for a run "
            f"ceiling of {max_tokens}; raise the run ceiling instead of the response cap")
    return requested


def bounded_limits(runtime: dict, tokens: int, seconds: float,
                   max_output_tokens_per_request: int | None = None) -> dict:
    if type(tokens) is not int or not 1 <= tokens <= MAX_RUN_TOKENS:
        raise ValueError(f"each run requires 1..{MAX_RUN_TOKENS} tokens")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= MAX_RUN_SECONDS:
        raise ValueError(f"each run requires a duration of at most {MAX_RUN_SECONDS} seconds")
    from minicode.unified_runtime import _finite_deadline, _optional_positive_float, _optional_positive_int
    saved_tokens = _optional_positive_int(runtime, "runtimeMaxTokens")
    ceiling = min(tokens, saved_tokens or tokens)
    return {"max_tokens": ceiling, "duration_seconds": seconds,
            # Fixed at registration time so every arm of a batch really shares it.
            "max_output_tokens_per_request": request_output_limit(
                ceiling, max_output_tokens_per_request),
            "max_cost": _optional_positive_float(runtime, "runtimeMaxCost"),
            "saved_deadline": _finite_deadline(runtime.get("runtimeDeadline"))}


def normalized(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: normalized(item, replacements) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(item, replacements) for item in value]
    if isinstance(value, str):
        for source, target in sorted(replacements.items(), key=lambda item: -len(item[0])):
            if source:
                value = value.replace(source, target).replace(source.replace("\\", "/"), target)
        return value
    return value


# The preparation steps a real Builder candidate lifecycle must have completed for
# the parallel mode to count as reachable. A receipt missing any of them did not
# demonstrate the mechanism, whatever it reports about itself.
_REQUIRED_PREPARATION_STEPS = ("candidate_created", "probe_written", "escape_refused",
                               "patch_exported", "candidate_released")


def builder_lifecycle_completed(receipt: Mapping[str, Any]) -> bool:
    """Whether the recorded preparation really completed the Builder lifecycle."""
    preparation = receipt.get("preparation")
    if not isinstance(preparation, Mapping):
        return False
    if not all(preparation.get(step) is True for step in _REQUIRED_PREPARATION_STEPS):
        return False
    if preparation.get("error"):
        return False
    if not preparation.get("patch_changed_paths") and "patch_changed_paths" in preparation:
        return False
    return True


def builder_capability_receipt(runtime: Mapping[str, Any]) -> dict[str, Any]:
    """What the primary's real tool view could reach for parallel Builder work.

    ``gateway_bound`` states whether the Runtime assembled a live BuilderGateway,
    ``tools_registered`` names the Builder tools that were really added to the
    primary's own scoped registry, and ``preparation`` is the receipt of the
    zero-model lifecycle exercise the assembly ran. Capacity alone never satisfies
    this: the model must have been *given* the writable-parallel action for the
    mode to be reachable at all, and the mode is only called reachable when the
    real candidate lifecycle behind those names completed.
    """
    registered = [name for name in (runtime.get("_builderCapabilityTools") or ())
                  if name in BUILDER_TOOL_NAMES]
    preparation = runtime.get("_builderCapabilityPrepare")
    preparation = dict(preparation) if isinstance(preparation, Mapping) else None
    handler = runtime.get("_builderCapabilityHandler")
    reachable = (
        runtime.get("_builderGatewayBound") is True
        and set(registered) == set(BUILDER_TOOL_NAMES)
        and builder_lifecycle_completed({"preparation": preparation}))
    return {
        "gateway_bound": runtime.get("_builderGatewayBound") is True,
        "tools_registered": registered,
        "enabled": runtime.get("runtimeParallelCandidates") is True,
        "handler": dict(handler) if isinstance(handler, Mapping) else None,
        "assembly_status": runtime.get("_runtimeAssemblyStatus"),
        "preparation": preparation,
        "reachable": reachable,
        "capacity": {"max_active_writers": runtime.get("runtimeMaxActiveWriters"),
                     "max_active_workers": runtime.get("runtimeMaxActiveWorkers")},
        "note": "capacity alone is not capability; a missing gateway, a missing "
                "Builder tool in the primary's own registry, or an unfinished real "
                "candidate lifecycle means the parallel mode cannot be reached by "
                "the model",
    }


def assembly_receipt(*, runtime, tools, messages, permissions, memory_manager,
                     replacements, limits, initial_files) -> dict:
    from minicode.unified_runtime import ProfileResolver
    from minicode.runtime_protocol import SelectedRoute
    request = next(str(item.get("content", "")) for item in reversed(messages) if item.get("role") == "user")
    profile = ProfileResolver().resolve(runtime=runtime, selected_route=SelectedRoute.PRIMARY_LED, request=request)
    policy = profile.to_dict()
    delegation = {key: policy.pop(key) for key in ("dynamic_worker_delegation", "autonomous_delegation")}
    # Concurrency is the arm's own treatment, not an input to compare.
    treatment = {key: policy.pop(key, None) for key in POLICY_TREATMENT_KEYS}
    policy["deadline"] = limits
    schemas = [{"name": tool.name, "description": tool.description, "schema": tool.input_schema,
                **tools.effect_manifest()[tool.name]} for tool in tools.list() if tool.name not in DELEGATION_TOOLS]
    # Config values can contain credentials. Only their digest leaves the
    # worker; private callbacks and per-run identities are not config inputs.
    # Keys reserved by the Runtime (leading underscore) are excluded for the same
    # reason they are reserved: they are assembled state and installed policy, not
    # operator configuration. Hashing live objects and handler identities into
    # "settings" made two arms look unequal for a difference the arm is *for*.
    effective = {key: value for key, value in runtime.items()
                 if not key.startswith("_")
                 and key not in {"_genericDelegationAllowed", "_sessionRawToolResult",
                                 "runtimeDeadline", *ARM_TREATMENT_KEYS}}
    effective["runtimeDeadline"] = limits
    parts = {"messages": messages, "non_delegation_tools": schemas,
             "permissions": permissions.get_summary(), "policy": policy,
             "effective_settings": effective, "initial_files": initial_files,
             "file_memory": {scope.value: [{key: value for key, value in entry.to_dict().items()
                                             if key not in {"last_accessed", "usage_count", "last_decay_at"}}
                                            for entry in memory.entries]
                             for scope, memory in memory_manager.memories.items()}}
    hashes = {key: digest(normalized(value, replacements)) for key, value in parts.items()}
    return {"schema": SCHEMA, "input_hashes": hashes, "comparable_hash": digest(hashes),
            "receipt_protocol_version": RECEIPT_PROTOCOL_VERSION,
            "delegation": delegation, "delegation_tools": sorted(set(tools.list_all()) & DELEGATION_TOOLS),
            "non_delegation_tool_count": len(schemas), "model": runtime.get("model"),
            "model_call_policy": runtime.get("_modelCallPolicy"),
            "profile": profile.requested_mode.value, "orchestration": "primary",
            "durable": runtime.get("runtimeDurableConsultation") is True,
            "independent_verifier_budget": profile.verifier_budget,
            # Effective concurrency the profile really resolved to, not the
            # capacity an arm name suggests.
            "effective_concurrency": {"max_active_workers": profile.max_active_workers,
                                      "max_active_writers": profile.max_active_writers,
                                      "parallel_candidates": profile.parallel_candidates,
                                      "effective_writer_capacity": profile.effective_writer_capacity},
            "arm_treatment": {"policy": treatment,
                              "settings": {key: runtime.get(key) for key in ARM_TREATMENT_KEYS
                                           if key in runtime}},
            "comparability_scope": {
                "compared": sorted(["messages", "non_delegation_tools", "permissions", "policy",
                                    "effective_settings", "initial_files", "file_memory"]),
                "treatment_excluded": [*POLICY_TREATMENT_KEYS, *ARM_TREATMENT_KEYS]},
            # Capability reachability, not just capacity. Enabling the parallel arm
            # without a bound gateway and registered Builder tools is a
            # configuration that cannot be reached by the model, and the comparison
            # refuses it instead of reporting an unused parallel capacity.
            "builder_capability": builder_capability_receipt(runtime)}


# The fields a live run must reproduce EXACTLY from the preflight that authorised
# it. These are the comparability INPUTS: the task, the tool schemas, the
# permissions, the resolved policy, the model route and the initial files. A run
# whose inputs moved after the preflight must not be admitted, because then the
# preflight did not describe the run that is about to happen.
#
# Deliberately NOT in this set, and why:
#   * ``builder_capability`` -- assembled state. A live run's Runtime is assembled
#     when the primary executes, which happens AFTER a model request is admitted, so
#     this receipt is necessarily observed at a different point in the two phases.
#     Gating on it compared "the preflight assembled a Runtime" against "the
#     post-prepare callback ran before execution", which is not input drift.
#   * ``effective_concurrency`` -- the arm's own treatment, already checked against
#     the arm name by ``compare_assemblies``.
ASSEMBLY_INPUT_FIELDS = (
    "input_hashes", "comparable_hash", "receipt_protocol_version", "state_input_hash",
    "delegation", "delegation_tools", "non_delegation_tool_count", "model",
    "model_call_policy", "profile", "orchestration", "durable",
    "independent_verifier_budget", "comparability_scope", "schema",
)


def assembly_input_drift(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Name every comparability input that moved between two assembly receipts."""
    drift: list[dict[str, Any]] = []
    for field in ASSEMBLY_INPUT_FIELDS:
        left, right = expected.get(field), actual.get(field)
        if left != right:
            drift.append({"field": field,
                          "expected": json.dumps(left, ensure_ascii=False, sort_keys=True)[:400],
                          "actual": json.dumps(right, ensure_ascii=False, sort_keys=True)[:400]})
    return drift


def compare_assemblies(left: dict, right: dict, *, right_arm: str = "autonomous_readonly",
                       left_arm: str = "primary_only") -> dict:
    reasons = []
    for key in set(left.get("input_hashes", {})) | set(right.get("input_hashes", {})):
        if left.get("input_hashes", {}).get(key) != right.get("input_hashes", {}).get(key):
            reasons.append("unequal_" + key)
    if not left.get("input_hashes") or not right.get("input_hashes"):
        reasons.append("missing_assembly")
    if left.get("state_input_hash") != right.get("state_input_hash"):
        reasons.append("unequal_state_seed")
    if left_arm == "primary_only":
        if left.get("delegation", {}).get("dynamic_worker_delegation") is not False or left.get("delegation_tools"):
            reasons.append("primary_only_not_enforced")
    elif left_arm in BUILDER_ARMS:
        reasons.extend("baseline_" + reason for reason in _autonomous_assembly_reasons(left, left_arm))
        if right_arm not in BUILDER_ARMS:
            raise ValueError("a Builder scheduling control requires two Builder-capable arms")
        if left.get("delegation_tools") != right.get("delegation_tools"):
            reasons.append("unequal_delegation_tools")
    else:
        raise ValueError("unknown comparison baseline")
    reasons.extend(_autonomous_assembly_reasons(right, right_arm))
    for item in (left, right):
        if item.get("profile") != "ultra" or not item.get("durable"):
            reasons.append("not_durable_ultra")
        if item.get("receipt_protocol_version") != RECEIPT_PROTOCOL_VERSION:
            reasons.append("stale_receipt_protocol_version")
    return {"valid": not reasons, "reasons": sorted(set(reasons)),
            "left_arm": left_arm, "right_arm": right_arm,
            "right_capacity": right.get("effective_concurrency", {}),
            "claim": "assemblies are comparable only for the concurrency their receipts report"}


def _autonomous_assembly_reasons(right: dict, right_arm: str) -> list[str]:
    reasons = []
    if right_arm not in AUTONOMOUS_ARMS:
        raise ValueError("the comparison's second arm must be an autonomous arm")
    if right.get("delegation", {}).get("dynamic_worker_delegation") is not True:
        reasons.append("autonomous_arm_cannot_delegate")
    if not set(right.get("delegation_tools", [])) & DELEGATION_TOOLS:
        reasons.append("production_tool_profile_has_no_delegation")
    expected = arm_capacity_expectation(right_arm)
    effective = right.get("effective_concurrency", {})
    if (effective.get("parallel_candidates") is not expected["parallel_candidates"]
            or effective.get("max_active_writers") != expected["max_active_writers"]
            or (right_arm in BUILDER_ARMS and effective.get("max_active_workers") != expected["max_active_workers"])):
        reasons.append("arm_concurrency_does_not_match_its_name")
    if expected["parallel_candidates"]:
        # Capacity is not capability. The arm is only a parallel-candidate arm if
        # the primary's real tool view contains every Builder tool AND the real
        # candidate lifecycle behind those tools completed during assembly; a
        # missing handler, a missing tool, or a preparation that did not finish
        # means the mechanism is not ready, whatever the numbers say.
        reachable = right.get("builder_capability", {})
        registered = set(reachable.get("tools_registered") or ())
        if reachable.get("gateway_bound") is not True or not registered:
            reasons.append("parallel_arm_has_no_reachable_builder_capability")
        elif registered != set(BUILDER_TOOL_NAMES):
            # The action the arm claims is the whole Builder surface. A subset is a
            # different, weaker capability and cannot stand in for it.
            reasons.append("parallel_arm_builder_tool_set_is_incomplete")
        # Reachability is recomputed from the recorded preparation, never read off
        # a self-reported flag: a receipt that claims reachability it did not
        # demonstrate is refused exactly like one that demonstrates nothing.
        if not builder_lifecycle_completed(reachable):
            reasons.append("parallel_arm_builder_lifecycle_did_not_complete")
        for name in registered:
            if name not in BUILDER_TOOL_NAMES:
                reasons.append("unregistered_parallel_tool")
    return reasons


def worker_execution_evidence(state: dict) -> dict:
    """Observed execution intervals, not registered capacity or lease lifetime."""
    starts, ends = {}, {}
    for event in state.get("events", []):
        payload = event.get("payload", {})
        key = (payload.get("node_id"), payload.get("attempt_id"))
        if not all(key):
            continue
        if event.get("event_type") == "AttemptStarted":
            starts[key] = event["created_at"]
        elif event.get("event_type") == "WorkerSettled":
            ends[key] = event["created_at"]
    intervals = [{"node_id": node, "attempt_id": attempt, "start": start,
                  "end": ends.get((node, attempt))} for (node, attempt), start in starts.items()]
    complete = bool(intervals) and starts.keys() == ends.keys() and all(
        item["end"] is not None and item["end"] >= item["start"] for item in intervals)
    active = builders = peak = builder_peak = 0
    events = [(at, delta, item["node_id"].startswith("builder-")) for item in intervals
              if item["end"] is not None for at, delta in ((item["start"], 1), (item["end"], -1))]
    for _at, delta, is_builder in sorted(events):
        active += delta
        builders += delta * is_builder
        peak, builder_peak = max(peak, active), max(builder_peak, builders)
    return {"complete": complete, "peak_workers": peak, "peak_builders": builder_peak,
            "intervals": intervals}


def trace_metrics(trace: dict | None, *, run_id: str, arm: str, model: str, model_policy: dict | None = None) -> dict:
    from minicode.runtime_delivery import entry_completion
    completion = entry_completion(trace, expected_run_id=run_id)
    trace = trace or {}
    state, scheduler = trace.get("state", {}), trace.get("scheduler", {})
    nodes = state.get("nodes", {})
    helpers = {key: value for key, value in nodes.items() if key not in {"primary", "final-verifier"}}
    calls = list(state.get("usage_ledger", {}).get("calls", {}).values())
    violations = []
    if arm == "primary_only" and (helpers or trace.get("profile", {}).get("dynamic_worker_delegation") is True):
        violations.append("primary_only_admission_violation")
    if arm == "primary_only" and trace.get("profile", {}).get("dynamic_worker_delegation") is None:
        violations.append("missing_admission_evidence")
    if arm in BUILDER_ARMS:
        # The arm claims a mode, not a result. Refuse to accept a receipt that
        # merely renamed the old single-writer run: the effective profile must
        # really have enabled parallel candidates.
        effective = trace.get("profile", {})
        if effective.get("parallel_candidates") is not True:
            violations.append("parallel_arm_ran_without_parallel_candidates")
        capacity = trace.get("capacity", {}) or trace.get("scheduler", {}).get("capacity", {})
        if arm == PARALLEL_ARM and capacity and int(capacity.get("max_writers", 0) or 0) < 2:
            violations.append("parallel_arm_had_no_second_writer_slot")
        if arm == SEQUENTIAL_BUILDER_ARM and (
            effective.get("max_active_workers") != 1 or effective.get("max_active_writers") != 1):
            violations.append("sequential_builder_arm_was_not_serial")
    builders = []
    for node_id, item in helpers.items():
        definition = item.get("definition", {})
        changes = (item.get("result") or {}).get("changed_resources") or []
        registered_builder = (arm in BUILDER_ARMS and trace.get("profile", {}).get("parallel_candidates") is True
            and node_id.startswith("builder-") and definition.get("role") == "writer"
            and state.get("delegation_parents", {}).get(node_id) == "primary")
        if not registered_builder:
            if definition.get("allowed_effects") != ["read_only"] or changes:
                violations.append("helper_write_violation")
            continue
        builders.append(node_id)
        if definition.get("allowed_effects") != ["read_only", "workspace_write"]:
            violations.append("helper_write_violation")
        scope = definition.get("write_scope") or []
        for resource in changes:
            if (not isinstance(resource, str) or not resource or "\\" in resource or ":" in resource
                or any(part in {"", ".", ".."} for part in resource.split("/"))
                or (scope and not any(resource == path or resource.startswith(path + "/") for path in scope))):
                violations.append("builder_write_scope_violation")
    by_purpose = {}
    for call in calls:
        identity = call.get("usage", {}).get("prepared_request", {}).get("call_identity", {})
        purpose = identity.get("purpose", "unknown")
        group = by_purpose.setdefault(purpose, {"calls": 0, "settled_calls": 0, "known_tokens": 0, "tokens_known": True})
        group["calls"] += 1
        group["settled_calls"] += call.get("status") == "settled"
        usage = call.get("usage", {})
        group["known_tokens"] += usage.get("tokens", 0)
        group["tokens_known"] = group["tokens_known"] and usage.get("tokens_known") is True and call.get("status") == "settled"
        if model_policy is not None:
            if not identity:
                violations.append("missing_call_identity")
            elif (model_policy.get(purpose) != {key: value for key, value in identity.items() if key != "purpose"}
                  or any(call.get(key) != identity.get(key) for key in ("model", "provider"))):
                violations.append("model_route_changed")
    # Legacy callers cannot retroactively assign a business purpose. Preserve
    # their conservative check; new runs require native call-site receipts.
    if model_policy is None and calls and any(call.get("model") != model for call in calls):
        violations.append("model_changed")
    if completion.reason != "runtime_completion_gate":
        violations.append(completion.reason)
    return {"completed": completion.completed, "completion_reason": completion.reason,
            "protocol_violations": sorted(set(violations)), "helper_nodes": len(helpers),
            "builder_nodes": len(builders), "readonly_helper_nodes": len(helpers) - len(builders),
            "builder_assignments": [{"node_id": n, **{key: nodes[n]["definition"].get(key) for key in
                ("objective", "write_scope", "dependencies", "acceptance_criteria", "criticality", "alternative_group")}}
                for n in sorted(builders)],
            "worker_execution": worker_execution_evidence(state),
            "builder_tools_seen_on_request": any(call.get("node_id") == "primary" and
                set(BUILDER_TOOL_NAMES) <= set(call.get("usage", {}).get("prepared_request", {}).get("runtime_tool_names", []))
                for call in calls),
            "model_calls_by_purpose": by_purpose,
            "independent_verifier_nodes": int("final-verifier" in nodes),
            "model_calls": len(calls), "all_calls_settled": all(c.get("status") == "settled" for c in calls),
            "tokens": scheduler.get("tokens_used"), "tokens_known": scheduler.get("tokens_known") is True,
            "cost": scheduler.get("cost_used") if scheduler.get("cost_known") is True else None,
            "cost_known": scheduler.get("cost_known") is True,
            "integration_status": state.get("integration_status"),
            "integration_revision": state.get("integration_revision"),
            "checks": state.get("final_verification_records", []),
            "delivery_reasons": trace.get("delivery", {}).get("reasons", []),
            "primary_activations": len(nodes.get("primary", {}).get("attempts", [])),
            "primary_reasoning": [item for item in trace.get("reasoning_budget_decisions", [])
                                  if item.get("node_id") == "primary"],
            "suspensions": sum(e.get("event_type") == "PrimarySuspended" for e in state.get("events", []))}


def compare_runs(left: dict, right: dict, *, right_arm: str = "autonomous_readonly",
                 left_arm: str = "primary_only") -> dict:
    """Compare one baseline against one autonomous arm.

    Kept pair-shaped on purpose: a plan recorded with the historical two arms
    still compares exactly as it did, and the newer parallel arm is compared the
    same way rather than being mixed into a three-way aggregate.
    """
    # A live Web preparation callback runs before gateway binding. Keep it as
    # input evidence, and use the separate zero-model preflight for its lifecycle
    # proof. Live binding and the actual request must independently confirm it.
    assembly_key = "preflight_assembly" if right_arm in BUILDER_ARMS else "assembly"
    comparison = compare_assemblies(left.get(assembly_key) or {}, right.get(assembly_key) or {},
                                    right_arm=right_arm, left_arm=left_arm)
    reasons = list(comparison["reasons"])
    if right_arm in BUILDER_ARMS:
        for name, run in ((left_arm, left), (right_arm, right)):
            if assembly_input_drift(run.get("preflight_assembly") or {}, run.get("assembly") or {}):
                reasons.append(name + "_assembly_inputs_changed")
            if name not in BUILDER_ARMS:
                continue
            live = run.get("live_capability") or {}
            if (live.get("run_id") != run.get("run_id") or not run.get("run_id")
                or live.get("stage") != "gateways_bound_before_first_model_request"
                or live.get("gateway_bound") is not True or live.get("enumerated") is not True
                or live.get("parallel_candidates") is not True
                or (live.get("gateway") or {}).get("scheduler_bound") is not True
                or not set(BUILDER_TOOL_NAMES) <= set(live.get("initial_model_tools") or [])):
                reasons.append("parallel_arm_missing_live_capability" if name == right_arm else "baseline_missing_live_capability")
            if run.get("metrics", {}).get("builder_tools_seen_on_request") is not True:
                reasons.append("parallel_arm_tools_not_observed_in_request" if name == right_arm else "baseline_tools_not_observed_in_request")
    if left_arm in BUILDER_ARMS:
        for field in ("builder_nodes", "readonly_helper_nodes", "independent_verifier_nodes"):
            if left.get("metrics", {}).get(field) != right.get("metrics", {}).get(field):
                reasons.append("unequal_" + field)
        if not left.get("metrics", {}).get("builder_nodes"):
            reasons.append("scheduling_comparison_has_no_builders")
        assignments = left.get("metrics", {}).get("builder_assignments")
        if not assignments or assignments != right.get("metrics", {}).get("builder_assignments"):
            reasons.append("unequal_builder_assignments")
        for name, run in ((left_arm, left), (right_arm, right)):
            execution = run.get("metrics", {}).get("worker_execution", {})
            if execution.get("complete") is not True:
                reasons.append(name + "_execution_intervals_incomplete")
            elif name == SEQUENTIAL_BUILDER_ARM and execution.get("peak_workers") != 1:
                reasons.append("sequential_workers_overlapped")
            elif name == PARALLEL_ARM and execution.get("peak_builders", 0) < 2:
                reasons.append("parallel_builders_did_not_overlap")
    for arm, run in zip((left_arm, right_arm), (left, right)):
        metrics = run.get("metrics", {})
        if run.get("error") or not metrics or metrics.get("protocol_violations"):
            reasons.append(arm + "_runtime_evidence_invalid")
        if not metrics.get("tokens_known") or not metrics.get("all_calls_settled"):
            reasons.append(arm + "_usage_unsettled")
        if run.get("acceptance", {}).get("checker_unchanged") is not True:
            reasons.append(arm + "_checker_changed")
    return {"valid": not reasons, "reasons": sorted(set(reasons)),
            "arms": [left_arm, right_arm],
            "claim": "one paired observation; retain both failures; no quality-improvement conclusion"}
