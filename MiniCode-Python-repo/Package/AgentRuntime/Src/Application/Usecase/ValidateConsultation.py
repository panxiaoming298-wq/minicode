"""Validate consultation ownership and capacity independently of mode names."""

from ..Dto.ConsultationPlan import ConsultationPlan


class ValidateConsultation:
    def execute(self, plan: ConsultationPlan) -> tuple[str, ...]:
        if plan.parent_role != "primary" or plan.parent_id not in plan.registered_ids:
            raise ValueError("only a registered primary may own consultations")
        if type(plan.max_waits) is not int or not 1 <= plan.max_waits <= 128:
            raise ValueError("max_waits must be an integer between 1 and 128")
        if type(plan.completed_waits) is not int or not 0 <= plan.completed_waits < plan.max_waits:
            raise ValueError("primary continuation budget exhausted")
        ids = tuple(child.node_id for child in plan.children)
        if not 1 <= len(ids) <= 8 or len(ids) != len(set(ids)) or plan.parent_id in ids:
            raise ValueError("consultation children must be nonempty, unique, and exclude their parent")
        if type(plan.max_agents) is not int or len(set(plan.registered_ids) | set(ids)) > plan.max_agents:
            raise ValueError("consultation exceeds max_agents, including reserved verification")
        for child in plan.children:
            if child.criticality not in {"required", "advisory"}:
                raise ValueError("consultation criticality must be required or advisory")
            if child.criticality == "advisory" and child.role not in {"explorer", "planner"}:
                raise ValueError("only exploration or planning advice can be advisory; review and verification are required")
            if not child.node_id.startswith("consult-") or child.dependencies:
                raise ValueError("consultation children must be independent nodes")
            if child.role not in {"explorer", "planner", "reviewer", "verifier"}:
                raise ValueError("unsupported consultation role")
            if child.effects != ("read_only",) or "read_only" not in plan.parent_effects:
                raise ValueError("consultations must remain inside the parent's read-only scope")
        return ids
