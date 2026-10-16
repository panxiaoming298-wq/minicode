"""Agent-facing regex search backed by the shared ripgrep service."""
from __future__ import annotations

from minicode.tooling import ToolContext, ToolDefinition, ToolResult
from minicode.tools import _file_search


def _run(input_data: dict, context: ToolContext) -> ToolResult:
    search = _file_search.search_files(input_data, context)
    result = search.to_tool_result()
    if search.native_file_observation is not None:
        from minicode.native_read_set import current_native_read_set

        observations = current_native_read_set()
        if observations is not None:
            path, content = search.native_file_observation
            observations.record_search_result(input_data, result, path, content)
    if search.native_directory_observation is not None:
        from minicode.native_read_set import current_native_read_set
        observations = current_native_read_set()
        if observations is not None:
            files, directories = search.native_directory_observation
            for path, content in files:
                observations.record_read(path, content)
            for path, digest in directories.items():
                observations.record_directory(path, digest=digest)
            observations.authenticate_search_result(input_data, result)
    return result


grep_files_tool = ToolDefinition(
    name="grep_files",
    description=(
        "Search one UTF-8 text file or files under a directory using ripgrep. "
        "Supports glob-based include/exclude filtering and context lines. "
        "Skips hidden/generated paths and common binary extensions; .gitignore is not applied. "
        "Results are formatted as path:line:content, capped at 200 matching lines and 50000 characters."
    ),
    input_schema=_file_search.SEARCH_INPUT,
    validator=_file_search.validate_input,
    run=_run,
)
