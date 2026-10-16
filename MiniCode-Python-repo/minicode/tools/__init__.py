import os
from dataclasses import asdict

from minicode.extension_tools import create_extension_tools
from minicode.extensions import extension_mcp_servers
from minicode.mcp import create_mcp_backed_tools
from minicode.skills import discover_skills
from minicode.tooling import ToolRegistry
from minicode.tools.ask_user import ask_user_tool
from minicode.tools.batch_ops import batch_copy_tool, batch_delete_tool, batch_move_tool
from minicode.tools.code_nav import (
    find_references_tool,
    find_symbols_tool,
    get_ast_info_tool,
)
from minicode.tools.code_review import code_review_tool
from minicode.tools.current_time import current_time_tool
from minicode.tools.diff_viewer import diff_viewer_tool
from minicode.tools.edit_file import edit_file_tool
from minicode.tools.file_tree import file_tree_tool
from minicode.tools.git import git_tool
from minicode.tools.grep_files import grep_files_tool
from minicode.tools.list_files import list_files_tool
from minicode.tools.load_skill import create_load_skill_tool, create_discover_skills_tool
from minicode.tools.patch_file import patch_file_tool
from minicode.tools.read_file import read_file_tool
from minicode.tools.display_image import display_image_tool
from minicode.tools.view_image import view_image_tool
from minicode.tools.record_task_checks import record_task_checks_tool
from minicode.tools.run_command import run_command_tool
from minicode.tools.development_services import create_service_tools
from minicode.tools.task import task_tool
from minicode.tools.test_runner import test_runner_tool
from minicode.tools.todo_write import todo_write_tool
from minicode.tools.web_fetch import web_fetch_tool
from minicode.tools.web_search import web_search_tool
from minicode.tools.write_file import write_file_tool

_CORE_TOOLS = [
    # User interaction
    ask_user_tool,
    # File operations
    list_files_tool,
    grep_files_tool,
    read_file_tool,
    display_image_tool,
    view_image_tool,
    record_task_checks_tool,
    write_file_tool,
    # modify_file_tool removed: identical to write_file (same _run/_validate)
    edit_file_tool,
    patch_file_tool,
    # Batch operations
    batch_copy_tool,
    batch_move_tool,
    batch_delete_tool,
    # Command execution
    run_command_tool,
    # Web tools
    web_fetch_tool,
    web_search_tool,
    # Local clock (avoids unnecessary network or shell calls for date/time)
    current_time_tool,
    # Task management
    todo_write_tool,
    # Sub-agent
    task_tool,
    # Git workflow
    git_tool,
    # Code intelligence
    find_symbols_tool,
    find_references_tool,
    get_ast_info_tool,
    code_review_tool,
    # Visualization
    file_tree_tool,
    diff_viewer_tool,
    # Testing
    test_runner_tool,
]

def _resolve_tool_profile(runtime: dict | None) -> str:
    configured = (
        os.environ.get("MINI_CODE_TOOL_PROFILE")
        or (runtime or {}).get("toolProfile")
        or "full"
    )
    return str(configured).strip().lower()


def _is_full_tool_profile(profile: str) -> bool:
    return profile in {"full", "utility", "utilities", "all"}


def _load_utility_wrapper_tools():
    # Keep explicit core-only sessions light. The full registry exposes these
    # capabilities to discovery; AgentLoop still defers their model schemas.
    from minicode.tools.archive_utils import (
        gzip_compress_tool,
        gzip_decompress_tool,
        tar_create_tool,
        tar_extract_tool,
        zip_create_tool,
        zip_extract_tool,
    )
    from minicode.tools.crypto_utils import hash_tool, hmac_tool, timestamp_tool
    from minicode.tools.csv_utils import csv_create_tool, csv_parse_tool
    from minicode.tools.encoding_utils import (
        base64_decode_tool,
        base64_encode_tool,
        url_decode_tool,
        url_encode_tool,
    )
    from minicode.tools.http_utils import http_request_tool
    from minicode.tools.json_utils import json_format_tool, json_parse_tool
    from minicode.tools.regex_utils import regex_replace_tool, regex_test_tool
    from minicode.tools.text_utils import (
        line_count_tool,
        random_string_tool,
        text_dedupe_tool,
        text_join_tool,
        text_sort_tool,
        uuid_generate_tool,
    )

    return [
        http_request_tool,
        json_format_tool,
        json_parse_tool,
        regex_test_tool,
        regex_replace_tool,
        base64_encode_tool,
        base64_decode_tool,
        url_encode_tool,
        url_decode_tool,
        timestamp_tool,
        hash_tool,
        hmac_tool,
        gzip_compress_tool,
        gzip_decompress_tool,
        tar_create_tool,
        tar_extract_tool,
        zip_create_tool,
        zip_extract_tool,
        csv_parse_tool,
        csv_create_tool,
        uuid_generate_tool,
        text_sort_tool,
        text_dedupe_tool,
        text_join_tool,
        line_count_tool,
        random_string_tool,
    ]


def create_builtin_tools(cwd: str, *, profile: str = "full", service_gateway=None):
    """Construct native definitions without connecting integrations or running tools."""

    tools = [*_CORE_TOOLS, *create_service_tools(service_gateway)]
    if _is_full_tool_profile(profile):
        tools.extend(_load_utility_wrapper_tools())
    tools.extend([create_load_skill_tool(cwd), create_discover_skills_tool(cwd)])
    return tools


def create_default_tool_registry(
    cwd: str,
    runtime: dict | None = None,
    *,
    inspection_only: bool = False,
    service_gateway=None,
) -> ToolRegistry:
    """Build the normal tool surface, optionally without active integrations.

    Inspection mode keeps declarative skills and external-tool descriptors
    visible, but it does not connect MCP servers or fire extension lifecycle
    hooks.  Product diagnostics use it so a read-only view cannot create
    processes, network sessions, or hook side effects.
    """

    from minicode.extension_agents import filter_tools_for_agent_profile
    from minicode.extension_hooks import activate_extension_hooks

    skills = [asdict(skill) for skill in discover_skills(cwd)]
    configured_mcp = {}
    if not inspection_only:
        configured_mcp = dict(runtime.get("mcpServers", {})) if runtime else {}
        configured_mcp.update(extension_mcp_servers(cwd))
    mcp = create_mcp_backed_tools(cwd=cwd, mcp_servers=configured_mcp)
    profile = _resolve_tool_profile(runtime)
    tools = create_builtin_tools(cwd, profile=profile, service_gateway=service_gateway)
    tools.extend(
        [
            *mcp["tools"],
            *create_extension_tools(cwd)["tools"],
        ]
    )
    tools = filter_tools_for_agent_profile(tools, runtime)
    hooks = (
        {"dispose": lambda: None}
        if inspection_only
        else activate_extension_hooks(cwd)
    )

    def dispose() -> None:
        try:
            hooks["dispose"]()
        finally:
            mcp["dispose"]()

    return ToolRegistry(
        tools,
        skills=skills,
        mcp_servers=mcp["servers"],
        disposer=dispose,
        skills_provider=lambda: [asdict(skill) for skill in discover_skills(cwd)],
    )
