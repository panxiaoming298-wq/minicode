"""Select review from concrete scope/risk signals, independently of mode depth."""

from ..Dto.ReviewPolicy import ReviewPolicy, ReviewPolicyRequest


class ResolveReviewPolicy:
    def execute(self, request: ReviewPolicyRequest) -> ReviewPolicy:
        if request.mode not in {"standard", "deep", "ultra"}:
            raise ValueError("invalid review execution mode")
        if request.explicit_review is not None and type(request.explicit_review) is not bool:
            raise ValueError("independent review preference must be boolean")
        if request.requested_scope not in {None, "basic", "focused", "full"}:
            raise ValueError("review scope must be basic, focused or full")
        if request.change_scope not in {"unknown", "none", "documentation", "single-file", "cross-module"}:
            raise ValueError("invalid change scope")
        if request.explicit_review is False:
            if request.requested_scope in {"focused", "full"}:
                raise ValueError("disabled review conflicts with requested review scope")
            return ReviewPolicy("basic", False, ("independent review explicitly disabled",))

        reasons = []
        high_risk = bool(request.risk_signals) or request.change_scope == "cross-module"
        if request.risk_signals:
            reasons.extend(request.risk_signals)
        if request.change_scope == "cross-module":
            reasons.append("cross-module behavior change")
        if request.multi_agent_integration:
            reasons.append("multiple agent contributions require integration review")
        if request.explicit_review is True:
            reasons.append("independent review explicitly required")
        scope = request.requested_scope or (
            "full" if high_risk or request.multi_agent_integration else
            "focused" if request.code_change or request.change_scope == "single-file" or request.explicit_review else
            "basic")
        required = (request.explicit_review is True or request.requested_scope in {"focused", "full"}
                    or high_risk or request.multi_agent_integration)
        if scope == "basic" and required:
            raise ValueError("basic scope conflicts with required independent review")
        if scope == "focused" and (high_risk or request.multi_agent_integration):
            raise ValueError("high-risk or integrated changes require full review")
        if not reasons:
            reasons.append("bounded code change" if scope == "focused" else
                           "requested review scope" if scope == "full" else
                           "necessary checks; no independent review signal")
        return ReviewPolicy(scope, required, tuple(reasons),
            reasoning_budget=request.reasoning_effort or ("xhigh" if scope == "full" else "medium"),
            runtime_profile="single-deep" if scope == "full" else "single")

    def unavailable_is_optional(self, policy: ReviewPolicy, *, state: str, failure_kind: str,
                                ambiguous_side_effect: bool = False, failed_verification: bool = False) -> bool:
        """An incomplete optional opinion never waives a finding or a real check."""
        return (state == "FAILED_TERMINAL" and not ambiguous_side_effect and not failed_verification
                and policy.scope != "basic" and not policy.required and failure_kind in {
                    "ReviewUnavailable", "BudgetExhausted", "ToolFailed", "ToolTimeout", "WorkerTimedOut",
                    "ModelOutputTruncated", "ProviderBalanceUnavailable",
                })
