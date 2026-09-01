# Comet Design Handoff

- Change: minicode-lite-productization
- Phase: design
- Mode: compact
- Context hash: ac56e14282b69f38edc73c75fe69a78377236f2e520f4d91a5573ba4d7f8138b

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## openspec/changes/minicode-lite-productization/proposal.md

- Source: openspec/changes/minicode-lite-productization/proposal.md
- Lines: 1-60
- SHA256: cd09872b8fdbf6cd45fafd45ed2dbc6ac5ddc0f0cf1cc4f747de676252ef3607

```md
## Why

`minicode` now has a strong runtime kernel, session replay, and checkpoint/rewind
surfaces, but it still feels like an advanced local runtime rather than a
lightweight Claude Code-style product. The next step is to turn the current
kernel into a coherent product surface by finishing the missing P1-P3 layers:
instruction governance, first-class delegation, extensibility, and release-grade
operator ergonomics.

## What Changes

- Add explicit instruction and policy layers so users can inspect which global,
  user, project, and machine-managed guidance was active in a turn.
- Turn hooks into first-class workflows with inspectable registration, lifecycle,
  async execution outcomes, and operator-facing UX in CLI/TUI/session artifacts.
- Productize delegated/background execution so subagents and long-running helper
  tasks are bounded, inspectable, replayable, and recoverable.
- Introduce a lightweight extension packaging model for local plugins/skills so
  `minicode` can share reusable commands and workflows without needing a heavy
  marketplace.
- Expand runtime evaluation and operator diagnostics into a release-readiness
  surface that can compare profiles, validate provider fallback behavior, and
  explain degraded states clearly.

## Capabilities

### New Capabilities

- `instruction-policy-layers`: Explicit instruction precedence, inspection, and
  managed policy loading for runtime turns and session artifacts.
- `first-class-hook-workflows`: Hook registration, visibility, async completion,
  and operator workflows that feel like a product surface instead of an internal
  API.
- `delegated-background-runtime`: Product-grade background and subagent
  execution with inspectable status, isolated outputs, and replayable summaries.
- `extension-packaging`: Lightweight local plugin/skill packaging, discovery,
  enablement, and shareable install flows.
- `product-readiness-evaluation`: Release-facing evaluation, provider-fallback
  diagnostics, and runtime health reporting that make `minicode-lite` operable.

### Modified Capabilities

- None.

## Impact

- Affected code:
  - `D:/Desktop/minicode/minicode/prompt.py`
  - `D:/Desktop/minicode/minicode/prompt_pipeline.py`
  - `D:/Desktop/minicode/minicode/config.py`
  - `D:/Desktop/minicode/minicode/hooks.py`
  - `D:/Desktop/minicode/minicode/background_tasks.py`
  - `D:/Desktop/minicode/minicode/agent_loop.py`
  - `D:/Desktop/minicode/minicode/session.py`
  - `D:/Desktop/minicode/minicode/cli_commands.py`
  - `D:/Desktop/minicode/minicode/tui/`
  - `D:/Desktop/minicode/minicode/runtime_profile_eval.py`
- New OpenSpec capability specs under
  `D:/Desktop/minicode/openspec/changes/minicode-lite-productization/specs/`
- New product-facing docs and build plans in `D:/Desktop/minicode/docs/superpowers/`
```

## openspec/changes/minicode-lite-productization/design.md

- Source: openspec/changes/minicode-lite-productization/design.md
- Lines: 1-161
- SHA256: fe1a609657eb4bc8298420be4807219d18f5da6a6300fc1e3f8cbfd658e0dc6c

[TRUNCATED]

```md
## Context

`minicode` has already closed most of the original P0 gap against a lightweight
Claude Code experience: runtime phases are explicit, widening and verification
are typed, sessions are persistent, and edits are recoverable through
checkpoint/rewind. The remaining gap is now product-surface coherence rather than
core loop invention.

The current codebase already contains the right primitives:

- instruction loading and prompt assembly:
  - `minicode/prompt.py`
  - `minicode/prompt_pipeline.py`
- settings and provider runtime validation:
  - `minicode/config.py`
- hook lifecycle and script execution:
  - `minicode/hooks.py`
- background task tracking:
  - `minicode/background_tasks.py`
- runtime control, events, and fallback behavior:
  - `minicode/agent_loop.py`
- session inspection, replay, checkpoints, and rewind:
  - `minicode/session.py`
- CLI/TUI command and visibility surfaces:
  - `minicode/cli_commands.py`
  - `minicode/tui/`
- runtime evaluation:
  - `minicode/runtime_profile_eval.py`

The productization work should therefore build on existing surfaces instead of
starting a parallel subsystem. The design also needs to respect the lightweight
goal: local-first, transparent, inspectable, and suitable for a single developer
or a small team without introducing heavy cloud or enterprise control planes.

## Goals / Non-Goals

**Goals:**

- Make active instruction sources explicit and inspectable during a run and after
  the session is saved.
- Upgrade hooks from an internal event registry into a visible workflow surface
  with outcome summaries and operator controls.
- Turn background and delegated execution into bounded product features with
  clean context hygiene, replayable outputs, and inspectable failure reasons.
- Introduce a lightweight extension packaging model for local plugins/skills.
- Add release-ready evaluation and provider/outage diagnostics that explain real
  runtime quality, not just unit test health.

**Non-Goals:**

- Building a full enterprise policy server or SaaS fleet manager.
- Recreating the complete Claude Code marketplace or remote platform.
- Adding broad IDE integrations in this change.
- Replacing the existing runtime kernel with a new architecture.

## Decisions

### Decision 1: Treat P1-P3 as capability layers on top of the existing runtime

The change will extend the current runtime and session model rather than
introducing a second product stack. This keeps checkpoint/replay/runtime-trace
behavior canonical and avoids split-brain UX across CLI, TUI, and saved session
artifacts.

Alternatives considered:

- Build a separate "product shell" around the runtime.
  - Rejected because it would duplicate state and weaken recovery/replay.
- Defer productization until a multi-agent rewrite exists.
  - Rejected because the current kernel is already strong enough to support a
    lightweight product.

### Decision 2: Model instruction governance as explicit layers, not hidden prompt text

Instruction loading will be represented as structured layers with precedence and
origin metadata instead of remaining implicit prompt text. The same structured
view should feed turn-time inspection, session artifacts, and future debugging.

Alternatives considered:

```

Full source: openspec/changes/minicode-lite-productization/design.md

## openspec/changes/minicode-lite-productization/tasks.md

- Source: openspec/changes/minicode-lite-productization/tasks.md
- Lines: 1-35
- SHA256: 0bbacb2b130292d8d286f785d11e9adf44c1d767f83c6e46e448d01487cc092a

```md
## 1. Instruction And Policy Layers

- [ ] 1.1 Define the managed policy file-path and precedence rules across global, user, project, and machine-managed instruction layers.
- [ ] 1.2 Add structured instruction-layer metadata to prompt assembly and runtime/session inspection outputs.
- [ ] 1.3 Expose instruction-layer inspection through CLI, TUI, and saved session replay surfaces.

## 2. Hook Workflow Productization

- [ ] 2.1 Add inspectable hook registry summaries with enabled state, event grouping, and recent execution health.
- [ ] 2.2 Record async hook completion and failure summaries into session/transcript artifacts without polluting normal assistant output.
- [ ] 2.3 Implement at least one first-class operator workflow for post-edit validation and one for delegated-task completion reporting.

## 3. Delegated Background Runtime

- [ ] 3.1 Define a typed delegated-runtime record that can represent background tasks and subagent-style isolated runs.
- [ ] 3.2 Surface live delegated status and replayable delegated summaries in CLI, TUI, and session artifacts.
- [ ] 3.3 Add delegated failure, retry, and recovery summaries that remain bounded and inspectable after session save.

## 4. Extension Packaging

- [ ] 4.1 Design and implement a lightweight local extension manifest format for plugins/skill bundles.
- [ ] 4.2 Add extension listing, enable/disable, and source inspection flows.
- [ ] 4.3 Document the local sharing/install workflow for extension bundles and connect it to product help surfaces.

## 5. Product Readiness Evaluation

- [ ] 5.1 Expand runtime evaluation outputs to include provider fallback and outage diagnostics alongside runtime profile metrics.
- [ ] 5.2 Add a release-readiness artifact that summarizes runtime health, delegated execution health, and required smoke checks.
- [ ] 5.3 Define and automate a minimal release gate covering session inspection, checkpoint/rewind, and provider fallback UX.

## 6. Verification And Rollout

- [ ] 6.1 Add targeted tests for new instruction-layer, hook, delegation, extension, and release-readiness behaviors.
- [ ] 6.2 Run full-repo verification plus product-style smoke checks and save the resulting artifact-backed report.
- [ ] 6.3 Update the product roadmap/report docs so the lightweight-Claude-Code positioning reflects the completed P1-P3 work.
```

## openspec/changes/minicode-lite-productization/specs/delegated-background-runtime/spec.md

- Source: openspec/changes/minicode-lite-productization/specs/delegated-background-runtime/spec.md
- Lines: 1-43
- SHA256: 4bfc4220ee8031f7b5dc6088328df173929e36c5b4dfbcec8e1b321c4537c9a2

```md
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
```

## openspec/changes/minicode-lite-productization/specs/extension-packaging/spec.md

- Source: openspec/changes/minicode-lite-productization/specs/extension-packaging/spec.md
- Lines: 1-29
- SHA256: 8a99187cb6b0b70e77a5b28ac83d4b75485d96d8e7c5cda86be62c027c327434

```md
## ADDED Requirements

### Requirement: Extensions have a lightweight local package format
The product SHALL support a local extension package format for plugins or skill
bundles that can be enabled, disabled, and shared without a remote registry.

#### Scenario: Extension is installed from a local bundle
- **WHEN** a user installs or enables a local extension bundle
- **THEN** the product records the bundle metadata, source path, and enabled
  state

#### Scenario: Extension is shared across repos or teammates
- **WHEN** a bundle is copied into another local environment
- **THEN** the receiving environment can inspect the same manifest metadata and
  enable the extension without manual code changes

### Requirement: Extension state is inspectable
The product SHALL expose extension enablement and source metadata through
inspectable product surfaces.

#### Scenario: User lists enabled extensions
- **WHEN** the user inspects product state
- **THEN** the output shows enabled extensions, bundle origin, and declared
  capabilities

#### Scenario: Extension is disabled
- **WHEN** a user disables an extension
- **THEN** the product updates its visible state and does not load the extension
  in future turns
```

## openspec/changes/minicode-lite-productization/specs/first-class-hook-workflows/spec.md

- Source: openspec/changes/minicode-lite-productization/specs/first-class-hook-workflows/spec.md
- Lines: 1-40
- SHA256: 380dfece6967ad1bf86a479754770b0d6b8643c8c6d70c757b2c5f01c8a14399

```md
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
```

## openspec/changes/minicode-lite-productization/specs/instruction-policy-layers/spec.md

- Source: openspec/changes/minicode-lite-productization/specs/instruction-policy-layers/spec.md
- Lines: 1-44
- SHA256: f1183b8beede9e3dbfcb6d94030145393986fb5dcb081cfa8e69a2fee2f5d93b

```md
## ADDED Requirements

### Requirement: Instruction sources are explicit and ordered
The runtime SHALL represent active instruction sources as named layers with a
stable precedence order rather than leaving them implicit in assembled prompt
text.

#### Scenario: Turn prompt is built
- **WHEN** a turn starts
- **THEN** the runtime records which instruction layers were loaded
- **AND** it records their source path or origin label
- **AND** it records the precedence order used to assemble them

#### Scenario: Layer order is inspected
- **WHEN** a user inspects the active session or a saved session
- **THEN** the product shows the active instruction layers in precedence order
- **AND** it distinguishes hand-authored policy from auto-loaded memory

### Requirement: Managed policy can be loaded separately from user and project guidance
The runtime SHALL support a machine-managed policy layer that is distinct from
global user guidance and project guidance.

#### Scenario: Managed policy file exists
- **WHEN** the runtime loads instructions
- **THEN** it loads the managed policy layer using a documented file path rule
- **AND** it records that the layer is machine-managed

#### Scenario: Managed policy conflicts with another layer
- **WHEN** multiple layers provide overlapping guidance
- **THEN** the runtime resolves them using the documented precedence order
- **AND** the inspection output shows which layer won

### Requirement: Instruction inspection is available in product surfaces
The product SHALL expose instruction-layer inspection in at least one CLI path,
one TUI path, and saved session artifacts.

#### Scenario: User inspects current session
- **WHEN** the user asks for session inspection during a run
- **THEN** the output includes active instruction layers and policy sources

#### Scenario: User replays a saved session
- **WHEN** the user inspects or replays a saved session
- **THEN** the output includes the instruction-layer summary that was active for
  that session snapshot
```

## openspec/changes/minicode-lite-productization/specs/product-readiness-evaluation/spec.md

- Source: openspec/changes/minicode-lite-productization/specs/product-readiness-evaluation/spec.md
- Lines: 1-39
- SHA256: d9a3398528a1e6d7753897fc15b60aac7e981944b23e38e09903932d56349ef7

```md
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
```

