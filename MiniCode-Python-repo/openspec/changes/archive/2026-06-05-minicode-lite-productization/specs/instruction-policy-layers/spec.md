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
