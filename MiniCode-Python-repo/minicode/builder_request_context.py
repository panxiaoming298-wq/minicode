"""Current Builder facts on the actual primary wire request, never a capability."""
from copy import deepcopy
from collections.abc import Mapping
import hashlib
import json

from minicode.candidate_patches import PATCH_ACCEPTED, PATCH_APPLIED, PATCH_APPLYING

POLICY = "native-primary-builder-state-v1"
PREFIX = "Runtime Builder state (read-only observations, not instructions or permission).\n"
MAX_BYTES = 32768


def merge_guidance(progress):
    if progress.get("status") == "candidate_busy":
        return ("The Runtime is preparing or mutating the candidate. This is a pending observation, "
                "not a model failure or permission to submit. Wait for existing Builder work; "
                "do not respawn it or infer a patch ID from a task ID.")
    if progress.get("status") == "checks_not_configured":
        return ("All required patches are applied, but no deterministic combination commands are configured. "
                "No check ran, so this is not a failing test or a pass. Review the merged candidate and use "
                "submit_candidate for mandatory independent final verification. Do not invent a check harness, "
                "recover valid patches, or request an unavailable run_acceptance_checks tool.")
    if progress["ready_to_submit"]:
        return ("All Builder patches are present and combination checks pass on the stated current revision. "
            "Earlier failed checks remain historical observations of their checked revisions; they do not "
            "describe these newer bytes. If your review is complete, call submit_candidate now to request "
            "registered acceptance and independent verification. No separate precheck is required. "
            "The registered harness may live outside the candidate: do not search for or reconstruct it "
            "with run_command. Reuse unchanged source observations; inspect only missing or changed evidence. "
            "This observation is not final acceptance; submission revalidates current state.")
    if progress["pending_application_task_ids"]:
        return ("Some Builder patches are not yet applied. Review and accept remaining required patches "
            "before diagnosing the complete combination; do not reapply an already applied patch. "
            "A failed partial-candidate check is still a failure, not proof that a later combination passes.")
    return ("Application receipts exist, but current whole-candidate checks or authority are not confirmed. "
        "Inspect the reported revisions and failures; do not reuse stale checks as a pass. "
        "submit_candidate invokes registered final checks when the candidate is ready; "
        "run_acceptance_checks is available through discover_tools for intermediate diagnostics.")


def packet(snapshot, invocation):
    ids = sorted(key for key, parent in snapshot["delegation_parents"].items()
        if parent == invocation.node_id and key.startswith("builder-"))
    registrations, leases = {}, {}
    for event in snapshot["events"]:
        payload = event["payload"]
        if event["event_type"] == "NodeReady" and payload.get("delegated"):
            registrations[payload["node_id"]] = payload.get("builder_idempotency_key")
        if event["event_type"] == "CandidateLeaseObserved":
            leases[payload["owner_node_id"]] = payload
    builders = []
    for key in ids:
        node = snapshot["nodes"][key]
        definition = node["definition"]
        lease = leases.get(key)
        patches = []
        for patch_id, record in snapshot["patch_records"].items():
            patch = record.get("patch") or {}
            if patch.get("owner_node_id") == key:
                checks = record.get("checks") or {}
                applied = record.get("state") == PATCH_APPLIED
                patches.append({"patch_id": patch_id, "state": record.get("state"),
                    "last_rejection": record.get("last_rejection"),
                    "owner_attempt_id": patch.get("owner_attempt_id"), "owner_generation": patch.get("owner_generation"),
                    "accepted": record.get("state") in {PATCH_ACCEPTED, PATCH_APPLYING, PATCH_APPLIED},
                    "applied": applied,
                    "checked": applied and bool(checks) and all(
                        isinstance(item, Mapping) and item.get("passed") is True for item in checks.values()),
                    "applied_revision": record.get("applied_revision"),
                    "dependency_vector": patch.get("dependency_vector")})
        builders.append({"task_id": key, "candidate_id": "candidate-" + key,
            "objective": definition["objective"],
            "idempotency_key": registrations.get(key), "state": node["state"],
            "attempt_id": node["current_attempt_id"], "cancel_epoch": snapshot["node_cancel_epoch"].get(key, 0),
            "write_scope": definition.get("write_scope", []), "alternative_group": definition.get("alternative_group"),
            "depends_on": definition["dependencies"], "predicates": snapshot["dependency_predicates"].get(key, {}),
            "failure": (node.get("result") or {}).get("failure"),
            "candidate_preparation": ((node.get("result") or {}).get("metrics") or {}).get("candidate_preparation"),
            "candidate_lease": {name: lease.get(name) for name in
                ("owner_attempt_id", "generation", "fencing_token", "released")} if lease else None,
            "patches": patches})
    recoveries = {event["payload"]["node_id"]: event["payload"]["builder_recovery"]
        for event in snapshot["events"] if event["event_type"] == "NodeReady" and event["payload"].get("builder_recovery")}
    return {"policy": POLICY, "run_id": snapshot["run_id"], "primary_attempt_id": invocation.attempt_id,
        "recoveries": recoveries,
        "plan_version": snapshot["current_plan_version"], "contract_revision": snapshot["contract_revision"],
        "state_revision": snapshot["state_revision"], "builders": builders,
        "selections": deepcopy(snapshot.get("candidate_selections", {})),
        "waits": [deepcopy(wait) for wait in snapshot["dependency_waits"].values()
            if wait.get("owner_node_id") in {invocation.node_id, *ids}],
        "grants_authority": False, "counts_as_acceptance": False}


class BuilderRequestView:
    """One request-local view. No authority is restored from model/history text."""
    def __init__(self, reducer, invocation, *, archive=None, merge_progress=None):
        self.reducer, self.invocation = reducer, invocation
        self.archive = archive
        self.merge_progress = merge_progress
        self.message = None
        self.diagnostic = None
        self.refresh()

    def refresh(self):
        facts = self.reducer.primary_builder_context(self.invocation)
        progress = self.merge_progress() if callable(self.merge_progress) else None
        detail = self.archive(facts) if callable(self.archive) else None
        if detail is not None:
            facts = deepcopy(facts)
            facts["view"] = "current-identities-and-paged-metadata"
            facts["complete_snapshot"] = detail
            for builder in facts["builders"]:
                objective = builder.pop("objective")
                builder["objective_preview"] = objective[:160]
                builder["objective_truncated"] = len(objective) > 160
                for patch in builder["patches"]:
                    patch.pop("dependency_vector")
            # Completed waits contain large release-proof copies. Retain their
            # identities and edge declarations inline; exact decisions/pins stay
            # available through the authenticated complete snapshot above.
            facts["waits"] = [{key: value for key, value in wait.items()
                              if key in {"wait_id", "owner_node_id", "owner_attempt_id", "edges", "mode", "failure_policy", "status", "timeout_seconds", "created_at"}}
                             for wait in facts["waits"]]
        if progress is not None:
            facts = {**facts, "current_merge": progress}
        reserve = self.reducer.closure_budget_snapshot()
        if reserve is not None:
            usage = self.reducer.usage_summary()
            cap = self.reducer.contract.budget.max_tokens
            threshold = None if cap is None else cap - (reserve["primary_tokens"] or 0) - (reserve["verification_tokens"] or 0)
            facts["budget"] = {"used_tokens": usage["tokens"], "reserved_tokens": usage["reserved_tokens"],
                "tokens_known": usage["tokens_known"], "max_tokens": cap, "closure_reserves": reserve,
                "finish_existing_work": threshold is not None and usage["tokens"] + usage["reserved_tokens"] >= threshold}
        encoded = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_BYTES:
            raise RuntimeError("current Builder state exceeds the request context limit; no task facts were truncated")
        self.message = {"role": "user", "content": PREFIX + encoded +
            "\nReuse existing task IDs; registration is not execution or success. Wait/poll existing work; "
            "do not re-register the same work under new keys. Deliberate alternatives remain supported. "
            "An idempotency key identifies the entire assignment; reuse requires identical objective, scope and criteria. "
            "Review actual Builder code with poll_task_results(patch_id=..., path=...) before accepting it. "
            "Candidate IDs are not filesystem paths; generic artifact_read/diff_viewer cannot resolve these IDs. "
            "States and patch receipts are observations at the stated revision, not current permission. "
            "Patch acceptance and tool authorization always revalidate current state."
            " For a rejected unmerged patch, use accept_builder_patch(patch_id=..., recover=true); "
            "do not repeat ordinary acceptance or invent new spawn keys. A recovery supersedes the old proposal, "
            "preserves its usage and requires reviewing and accepting the replacement's new patch."
            " Recovery requires a real patch in this inventory. If candidate preparation failed before the Worker "
            "started, no patch exists: native setup already made bounded I/O retries. Report the actual setup "
            "failure without inventing patch IDs or trying Coordinator writes; an incomplete run remains failed."
            " When finish_existing_work is true, prioritize merging, fixing required checks and submit_candidate; "
            "avoid optional exploration and additional delegation. Required independent verification remains mandatory."
            + ("\n" + merge_guidance(progress) if progress is not None else "")}
        self.diagnostic = {"policy": POLICY, "state_revision": facts["state_revision"],
            "run_id": facts["run_id"], "primary_attempt_id": facts["primary_attempt_id"],
            "builder_ids": [item["task_id"] for item in facts["builders"]],
            "snapshot_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "message_utf8_bytes": len(self.message["content"].encode("utf-8")),
            "grants_authority": False, "counts_as_acceptance": False}

    def attach(self, body, *, refresh=False):
        # Remove only the exact object previously inserted by this request.
        # A model/user string with the same prefix is not Runtime ownership.
        messages = [item for item in body.get("messages", []) if item is not self.message]
        if refresh:
            self.refresh()
        return {**body, "messages": [*messages, self.message]}

    def detach(self, body):
        return {**body, "messages": [item for item in body.get("messages", []) if item is not self.message]}

    def validate(self, body):
        if sum(item is self.message for item in body.get("messages", [])) != 1:
            raise RuntimeError("current Builder facts were removed or duplicated after request preparation")
