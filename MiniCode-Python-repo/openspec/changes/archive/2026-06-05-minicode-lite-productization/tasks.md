## 1. Instruction And Policy Layers

- [x] 1.1 Define the managed policy file-path and precedence rules across global, user, project, and machine-managed instruction layers.
- [x] 1.2 Add structured instruction-layer metadata to prompt assembly and runtime/session inspection outputs.
- [x] 1.3 Expose instruction-layer inspection through CLI, TUI, and saved session replay surfaces.

## 2. Hook Workflow Productization

- [x] 2.1 Add inspectable hook registry summaries with enabled state, event grouping, and recent execution health.
- [x] 2.2 Record async hook completion and failure summaries into session/transcript artifacts without polluting normal assistant output.
- [x] 2.3 Implement at least one first-class operator workflow for post-edit validation and one for delegated-task completion reporting.

## 3. Delegated Background Runtime

- [x] 3.1 Define a typed delegated-runtime record that can represent background tasks and subagent-style isolated runs.
- [x] 3.2 Surface live delegated status and replayable delegated summaries in CLI, TUI, and session artifacts.
- [x] 3.3 Add delegated failure, retry, and recovery summaries that remain bounded and inspectable after session save.

## 4. Extension Packaging

- [x] 4.1 Design and implement a lightweight local extension manifest format for plugins/skill bundles.
- [x] 4.2 Add extension listing, enable/disable, and source inspection flows.
- [x] 4.3 Document the local sharing/install workflow for extension bundles and connect it to product help surfaces.

## 5. Product Readiness Evaluation

- [x] 5.1 Expand runtime evaluation outputs to include provider fallback and outage diagnostics alongside runtime profile metrics.
- [x] 5.2 Add a release-readiness artifact that summarizes runtime health, delegated execution health, and required smoke checks.
- [x] 5.3 Define and automate a minimal release gate covering session inspection, checkpoint/rewind, and provider fallback UX.

## 6. Verification And Rollout

- [x] 6.1 Add targeted tests for new instruction-layer, hook, delegation, extension, and release-readiness behaviors.
- [x] 6.2 Run full-repo verification plus product-style smoke checks and save the resulting artifact-backed report.
- [x] 6.3 Update the product roadmap/report docs so the lightweight-Claude-Code positioning reflects the completed P1-P3 work.
