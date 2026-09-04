from __future__ import annotations

from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from minicode.tools import create_default_tool_registry


def test_default_tool_effect_manifest_is_explicit_and_explorer_filter_is_read_only(tmp_path) -> None:
    registry = create_default_tool_registry(str(tmp_path), runtime={"mcpServers": {}})
    manifest = registry.effect_manifest()
    assert manifest
    assert manifest["read_file"]["effect"] == "read_only"
    assert manifest["write_file"]["effect"] == "workspace_write"
    assert manifest["run_command"]["effect"] == "privileged"
    assert manifest["web_fetch"]["effect"] == "external_side_effect"
    assert manifest["task"]["replay_policy"] == "idempotent_with_key"

    explorer = registry.filtered_by_effects({ToolEffect.READ_ONLY})
    assert explorer.list()
    assert all(tool.effect is ToolEffect.READ_ONLY for tool in explorer.list())
    assert explorer.find("write_file") is None

    without_network = registry.filtered_without_names({"web_search", "web_fetch"})
    assert without_network.find("web_search") is None
    assert without_network.find("web_fetch") is None
    assert without_network.find("read_file") is not None


def test_unknown_tool_fails_closed_as_privileged_and_policy_denies_it(tmp_path) -> None:
    tool = ToolDefinition(
        name="extension_unknown",
        description="unknown extension",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda value, context: ToolResult(ok=True, output="ran"),
    )
    registry = ToolRegistry([tool])
    assert tool.effect is ToolEffect.PRIVILEGED
    assert tool.replay_policy is ReplayPolicy.NEVER_AUTO_RETRY
    result = registry.execute(
        tool.name,
        {},
        ToolContext(cwd=str(tmp_path), allowed_effects=frozenset({ToolEffect.READ_ONLY})),
    )
    assert not result.ok
    assert result.output.startswith("PolicyDenied:")
