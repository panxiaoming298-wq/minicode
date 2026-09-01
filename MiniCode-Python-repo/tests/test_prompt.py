from pathlib import Path

from minicode.prompt import build_system_prompt


def test_build_system_prompt_includes_skills_and_mcp(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        ["cwd: test"],
        {
            "skills": [{"name": "demo", "description": "demo skill"}],
            "mcpServers": [{"name": "fake", "status": "connected", "toolCount": 1, "resourceCount": 1, "promptCount": 1, "protocol": "newline-json"}],
        },
    )

    assert "Available skills:" in prompt
    assert "demo skill" in prompt
    assert "Configured MCP servers:" in prompt
    assert "fake: connected, tools=1" in prompt


def test_build_system_prompt_mentions_sequential_thinking_server(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        [],
        {
            "mcpServers": [
                {"name": "SequentialThinking", "status": "connected", "toolCount": 1}
            ]
        },
    )

    assert "SEQUENTIAL THINKING MCP SERVER IS CONNECTED" in prompt
    assert "sequential_thinking" in prompt


def test_build_system_prompt_includes_memory_context(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        [],
        {"memory_context": "# Project Memory\n\n- Always run pytest before release."},
    )

    assert "Project Memory & Context" in prompt
    assert "Always run pytest before release." in prompt


# ---------------------------------------------------------------------------
# Robustness: the system-prompt builder runs every turn; malformed MCP/skill/
# permission inputs must not crash it.
# ---------------------------------------------------------------------------


def test_build_system_prompt_bundle_handles_malformed_mcp_entry():
    """A partial MCP server dict (missing toolCount/name/status) or a non-dict
    entry must not KeyError/AttributeError the prompt build."""
    from minicode.prompt import build_system_prompt_bundle

    extras = {
        "mcpServers": [
            {"name": "broken", "status": "error"},  # missing toolCount
            "not-a-dict",  # wholly malformed
            {"name": "sequential-thinking", "status": "connected", "toolCount": 2},
        ],
        "skills": [],
        "memory_context": "",
        "runtime": {},
    }
    bundle = build_system_prompt_bundle(".", ["cwd: ."], extras)
    assert isinstance(bundle.prompt, str) and bundle.prompt
    assert "sequential-thinking" in bundle.prompt


def test_build_system_prompt_bundle_handles_none_in_permission_summary():
    """A None element in permission_summary must not crash join()."""
    from minicode.prompt import build_system_prompt_bundle

    bundle = build_system_prompt_bundle(
        ".", ["ok", None, "x"], {"mcpServers": [], "skills": [], "memory_context": "", "runtime": {}}
    )
    assert "Permission context" in bundle.prompt
    assert "ok" in bundle.prompt and "x" in bundle.prompt


def test_build_system_prompt_bundle_handles_malformed_skill():
    """A skill dict missing name/description must not KeyError the build."""
    from minicode.prompt import build_system_prompt_bundle

    bundle = build_system_prompt_bundle(
        ".", [], {"mcpServers": [], "skills": [{"name": "s"}], "memory_context": "", "runtime": {}}
    )
    assert isinstance(bundle.prompt, str) and bundle.prompt
