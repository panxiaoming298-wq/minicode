## ADDED Requirements

### Requirement: Hook registrations are inspectable
The product SHALL expose registered hooks, their enabled state, and their recent
execution health as a user-facing workflow surface.

#### Scenario: Hook status is requested
- **WHEN** a user inspects hook state from CLI, TUI, or session artifacts
- **THEN** the product lists registered hooks by lifecycle event
- **AND** it shows whether each hook is enabled
- **AND** it shows recent call counts or health summaries

### Requirement: Async hook outcomes are summarized
The product SHALL report hook outcomes without forcing users to read raw script
or callback chatter.

#### Scenario: Async hook completes successfully
- **WHEN** an async hook finishes
- **THEN** the runtime records a summarized outcome tied to the originating event
- **AND** the summary can be shown in the session timeline or replay view

#### Scenario: Async hook fails
- **WHEN** an async hook errors or times out
- **THEN** the runtime records the failure reason
- **AND** it does not crash the main runtime
- **AND** the operator can inspect the failure later

### Requirement: Hook workflows support common operator automation patterns
The hook system SHALL support user-facing workflows such as post-edit tests and
background completion summaries.

#### Scenario: Post-edit test workflow is configured
- **WHEN** a file-editing event completes
- **THEN** a configured hook can run an associated validation step
- **AND** the product records the resulting success or failure summary

#### Scenario: Background task completion hook is configured
- **WHEN** a delegated or background task completes
- **THEN** a configured hook can emit a structured summary back into the main
  session timeline
