"""Governed compatibility boundary for the ``task`` tool."""

from __future__ import annotations

from dataclasses import dataclass

from minicode.runtime_protocol import AgentResult, DelegationRequest
from minicode.runtime_scheduler import (
    ContextBuilder,
    NodeVerifier,
    TaskScheduler,
    Worker,
)


@dataclass(slots=True)
class SchedulerDelegationGateway:
    scheduler: TaskScheduler
    worker: Worker
    context_builder: ContextBuilder
    node_verifier: NodeVerifier

    @property
    def run_id(self) -> str:
        return self.scheduler.reducer.run_id

    @property
    def max_delegation_depth(self) -> int:
        return self.scheduler.profile.max_delegation_depth

    @property
    def dynamic_worker_delegation(self) -> bool:
        return self.scheduler.profile.dynamic_worker_delegation

    def submit(self, request: DelegationRequest) -> AgentResult:
        if request.run_id != self.run_id:
            raise ValueError("DelegationRequest run_id does not match active Runtime")
        return self.scheduler.execute_delegation(
            request,
            worker=self.worker,
            context_builder=self.context_builder,
            node_verifier=self.node_verifier,
        )
