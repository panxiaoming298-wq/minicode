"""Bounded access to the current session's independently retained raw history."""
from __future__ import annotations

import json

from minicode.history_storage import history_operation
from minicode.history_objects import expand
from minicode.session_contract import _valid_session_id
from minicode.session_history import HistoryError, _index, _snapshot
from minicode.store_lock import serialized_sessions
from minicode.tooling import ToolCapability, ToolDefinition, ToolMetadata, ToolResult


def _validate(value):
    from minicode.tools._validation import validate_contract
    validate_contract(value, {"type": "object", "additionalProperties": False, "properties": {
        "turnId": {"type": "string"}, "itemId": {"type": "string", "maxLength": 256},
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 16000}}})
    turn_id = value.get("turnId", "")
    if not isinstance(turn_id, str) or (turn_id and not _valid_session_id(turn_id)):
        raise ValueError("invalid turnId")
    offset, limit = value.get("offset", 0), value.get("limit", 8000)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
        raise ValueError("offset must be nonnegative and limit must be 1..16000")
    item_id = value.get("itemId", "")
    if item_id and turn_id:
        raise ValueError("provide itemId or turnId, not both")
    return {"turnId": turn_id, "itemId": item_id, "offset": offset, "limit": limit}


@serialized_sessions
@history_operation(read_only=True)
def _run(value, context):
    if context.session is None:
        return ToolResult(ok=False, output="This execution has no session history.")
    # Scope is taken only from the running session, never from model input.
    session_id = context.session.session_id
    if value.get("itemId"):
        from minicode.session_history_query import read_item
        item = read_item(session_id, value["itemId"])
        text = item.pop("content")
        offset, limit = value["offset"], value["limit"]
        end = min(len(text), offset + limit)
        return ToolResult(ok=True, output=json.dumps({**item, "offset": offset, "totalChars": len(text),
            "nextOffset": end if end < len(text) else None, "content": text[offset:end]}, ensure_ascii=False))
    rows = _index(session_id)
    if value["turnId"]:
        if not any(row["turnId"] == value["turnId"] and row["status"] != "running" for row in rows):
            raise HistoryError("该轮次不属于当前会话的已保存历史。")
        snapshot = _snapshot(session_id, value["turnId"], display_only=True)
        fields = {"role", "content", "toolName", "toolUseId", "input", "isError"}
        document = {"messages": [{key: item for key, item in message.items() if key in fields}
                                  for message in snapshot["displayMessages"]],
                    "rawToolResults": expand(snapshot.get("rawToolsRef")) if snapshot.get("formatVersion") == 2
                                      else snapshot.get("rawToolResults", [])}
    else:
        document = [{key: row.get(key) for key in ("turnId", "prompt", "status")}
                    for row in rows if row["status"] != "running"]
    text = json.dumps(document, ensure_ascii=False, indent=2)
    offset, limit = value["offset"], value["limit"]
    end = min(len(text), offset + limit)
    return ToolResult(ok=True, output=json.dumps({
        "turnId": value["turnId"], "offset": offset, "totalChars": len(text),
        "nextOffset": end if end < len(text) else None, "content": text[offset:end],
    }, ensure_ascii=False))


session_history_read_tool = ToolDefinition(
    name="session_history_read",
    description=("Read independently saved conversation history and original tool outputs in this session. "
                 "Omit turnId to list saved turns; supply turnId to read its raw display messages. "
                 "Use this for old Artifact IDs from a parent task; never replay historical tool calls. "
                 "Use itemId from session_history_search to read one original item's text with matching character offsets. "
                 "Paginate character ranges with offset/limit."),
    input_schema={"type": "object", "properties": {
        "turnId": {"type": "string"}, "itemId": {"type": "string", "maxLength": 256},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 16000}}, "additionalProperties": False},
    validator=_validate, run=_run,
    metadata=ToolMetadata(name="session_history_read", description="Read this session's saved history",
                          capabilities={ToolCapability.READ_ONLY}, max_result_size_chars=20000),
)
