## ADDED Requirements

### Requirement: Release-readiness combines runtime and provider health evidence
The evaluation surface SHALL report both local runtime quality and provider
availability behavior when judging readiness.

#### Scenario: Release-readiness evaluation is run
- **WHEN** a release-style evaluation is executed
- **THEN** the output includes runtime profile metrics, delegated/runtime-event
  metrics, and provider fallback or outage outcomes

#### Scenario: Provider availability is degraded
- **WHEN** the active provider or fallback chain is unavailable
- **THEN** the readiness output records the degraded state explicitly
- **AND** it distinguishes provider outage from local logic failure

### Requirement: Evaluation outputs are product-readable
The evaluation layer SHALL produce artifacts that operators can inspect without
reading raw traces only.

#### Scenario: Evaluation report is generated
- **WHEN** runtime evaluation completes
- **THEN** the product emits structured machine-readable data
- **AND** it emits a human-readable summary that explains completion, widening,
  guard triggers, and stop reasons

### Requirement: Release gates can require targeted smoke coverage
The product SHALL support a minimal release gate that requires targeted smoke
checks for critical workflows.

#### Scenario: Release gate is configured
- **WHEN** a release-readiness run is executed
- **THEN** it can require targeted smoke checks for at least session inspection,
  checkpoint/rewind, and provider fallback UX

#### Scenario: Release gate fails
- **WHEN** a required smoke check or provider fallback diagnostic fails
- **THEN** the readiness output marks the release as not ready
- **AND** it reports which gate failed
