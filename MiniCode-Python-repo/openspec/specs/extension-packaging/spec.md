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
