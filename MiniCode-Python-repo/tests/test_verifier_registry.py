from __future__ import annotations

import sys

from minicode.runtime_protocol import PlanNode, PlanVersion, TaskContract, TaskEnvelope
from minicode.verifier_registry import (
    IsolatedCommandVerifier,
    VerificationContext,
    VerifierRegistry,
)


def _context(tmp_path) -> VerificationContext:
    contract = TaskContract(
        envelope=TaskEnvelope(
            run_id="run-1",
            request="verify",
            workspace=str(tmp_path),
        )
    )
    plan = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "verify", "standard"),),
    )
    return VerificationContext(contract, plan, tmp_path, final=True)


def test_command_verifier_runs_on_integrated_snapshot_without_polluting_source(tmp_path) -> None:
    (tmp_path / "input.txt").write_text("integrated", encoding="utf-8")
    verifier = IsolatedCommandVerifier(
        name="unit",
        kind="unit_test",
        command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "assert Path('input.txt').read_text(encoding='utf-8') == 'integrated'; "
                "Path('verifier-cache.txt').write_text('temporary', encoding='utf-8')"
            ),
        ),
    )
    records = VerifierRegistry((verifier,)).run(_context(tmp_path))

    assert records[0].passed
    assert "exit_code=0" in records[0].details
    assert not (tmp_path / "verifier-cache.txt").exists()


def test_command_verifier_failure_is_a_failed_record(tmp_path) -> None:
    verifier = IsolatedCommandVerifier(
        name="integration",
        kind="integration_test",
        command=(sys.executable, "-c", "raise SystemExit(7)"),
    )
    record = VerifierRegistry((verifier,)).run(_context(tmp_path))[0]
    assert not record.passed
    assert "exit_code=7" in record.details

