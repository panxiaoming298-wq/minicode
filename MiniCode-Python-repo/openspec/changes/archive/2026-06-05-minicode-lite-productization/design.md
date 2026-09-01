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

- Keep layering implicit and only improve docs.
  - Rejected because the user experience problem is observability, not
    documentation.
- Introduce organization-only policy support first.
  - Rejected because this product is meant to stay lightweight and local-first.

### Decision 3: Productize hooks and delegated runtime through shared event/state summaries

Hooks, background tasks, and subagents should publish a common state summary that
can appear in CLI/TUI/session replay. This avoids each feature inventing its own
inspection story and keeps context hygiene enforceable.

Alternatives considered:

- Let hooks stay internal while only productizing subagents.
  - Rejected because hooks are already part of the runtime lifecycle and need
    user-visible trust boundaries.
- Add background execution first without replay/state unification.
  - Rejected because it would create opaque async behavior.

### Decision 4: Keep extension packaging local-first and file-based

The initial plugin/skill packaging layer should use local manifests, local
enable/disable state, and repo-sharable bundles. That yields most of the
lightweight-Claude-Code value without requiring a marketplace.

Alternatives considered:

- Build a remote registry from the start.
  - Rejected as too heavy for the current product scope.
- Keep extensions as undocumented folder conventions.
  - Rejected because packaging and inspectability are the missing product layer.

### Decision 5: Release readiness must include provider-fallback truth, not only tests

The evaluation layer will combine runtime profile comparisons, provider fallback
diagnostics, and operator-facing degraded-state reporting. This is necessary
because the recent real-run bottlenecks were provider-availability failures, not
logic bugs.

Alternatives considered:

- Limit release gating to unit/integration test pass rates.
  - Rejected because it misses the most visible user-facing failures.
- Push provider diagnostics into a separate operational project.
  - Rejected because runtime trust depends on it.

## Risks / Trade-offs

- [Scope sprawl] -> Keep the work grouped into capability specs with explicit
  non-goals and a build plan that sequences P1 before P2/P3 polish.
- [Too much new UX at once] -> Reuse existing session, transcript, and runtime
  summary surfaces instead of introducing brand-new panels everywhere.
- [Delegation pollutes context] -> Require isolated result summaries and bounded
  replay artifacts before delegated runtime is considered complete.
- [Plugin packaging becomes over-engineered] -> Start with local manifests and
  repo-sharable bundles only; defer remote registry concerns.
- [Evaluation becomes disconnected from real usage] -> Keep benchmark outputs and
  real provider/outage diagnostics in the same release-readiness surface.

## Migration Plan

1. Land OpenSpec capability specs and a build plan for P1-P3.
2. Implement instruction-policy inspection and hook workflows first so later
   delegated/runtime features have a stable governance surface.
3. Implement delegated/background runtime and extension packaging with shared
   session/replay visibility.
4. Finish product-readiness evaluation and release diagnostics once the new
   runtime surfaces are emitting structured state.
5. Verify with targeted product workflows, full repo tests, and release-style
   smoke artifacts before closing the change.

## Open Questions

- Should managed policy live in a dedicated file path under the user profile,
  repo root, or both?
- Should delegated runtime be one generalized task surface, or a thin subagent
  layer on top of background tasks plus current task tooling?
- What is the minimal manifest format for local extension packaging that still
  supports inspection and reproducibility?
- Which provider fallback checks must be mandatory in release-readiness gating?
