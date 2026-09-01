## ADDED Requirements

### Requirement: Delegated executions are bounded and typed
The product SHALL model delegated work as typed runtime units with bounded
context, status, and result contracts.

#### Scenario: Delegated task starts
- **WHEN** the main runtime launches background or subagent work
- **THEN** the delegated unit records its type, task description, start time,
  status, and context mode

#### Scenario: Delegated task uses isolated context
- **WHEN** the delegated unit is configured for isolated execution
- **THEN** only its final summary and durable artifacts flow back into the main
  session by default

### Requirement: Delegated status is inspectable while running and after completion
The product SHALL let users inspect delegated runtime units during execution and
after the session is saved.

#### Scenario: User checks live delegated status
- **WHEN** delegated work is running
- **THEN** the CLI or TUI shows active delegated units, status, and latest
  progress or result summary

#### Scenario: User replays a completed session
- **WHEN** a saved session contains delegated runtime activity
- **THEN** session replay includes delegated start, completion, failure, and
  retry summaries

### Requirement: Delegated failures are visible and recoverable
The product SHALL record delegated failure reasons and provide enough state to
retry or diagnose them without polluting the main context.

#### Scenario: Delegated unit fails
- **WHEN** delegated work errors, stalls, or is cancelled
- **THEN** the runtime records the failure reason and terminal status
- **AND** the saved session preserves that outcome for later inspection

#### Scenario: Delegated unit is retried
- **WHEN** the operator retries delegated work
- **THEN** the product distinguishes the retried run from the original run
- **AND** the replay surface can show both outcomes
