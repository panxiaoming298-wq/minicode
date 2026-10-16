"""Model tool for literal search and browsing of the current session's saved turns."""
import json

from minicode.history_query import SEARCH_PROPERTIES, validate_search
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.policy import TokenBudgetManager
from minicode.session_history_query import search_items
from minicode.tooling import ToolCapability, ToolDefinition, ToolMetadata, ToolResult


EXTRA_PROPERTIES = {name: {"type": "string", "maxLength": 256} for name in ("turn_id", "node_id")}


def _validate(value):
    return {"turn_id": "", "node_id": "", **validate_search(value, EXTRA_PROPERTIES)}


def _run(value, context):
    if context.session is None:
        return ToolResult(ok=False, output="This execution has no session history.")
    runtime = context._runtime or {}
    config = PagedMemoryConfig.from_sources(runtime, cwd=context.cwd)
    budget = TokenBudgetManager(model_name=str(runtime.get("model", "")))
    result = search_items(context.session.session_id, value, token_budget=budget,
                          token_limit=config.raw_page_token_limit,
                          timeout=config.retrieval_timeout_seconds)
    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))


session_history_search_tool = ToolDefinition(name="session_history_search", description=(
    "Search literal original text in this session's saved public messages and tool results, or omit query to browse. "
    "Case-sensitive by default; filter turn/role/tool/node and choose recent_first. Follow next_cursor with the same "
    "filters even after an empty page; read returned item_id with session_history_read. No note or source ID is required. "
    "Running turns and private model context are excluded; history_search handles active task events."),
    input_schema={"type": "object", "additionalProperties": False,
                  "properties": {**SEARCH_PROPERTIES, **EXTRA_PROPERTIES}},
    validator=_validate, run=_run, metadata=ToolMetadata(name="session_history_search",
        description="Search this session's saved history", capabilities={ToolCapability.READ_ONLY}, max_result_size_chars=20000))
