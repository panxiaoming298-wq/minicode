from __future__ import annotations

import time
from typing import Any

from minicode.tooling import ToolDefinition, ToolResult
from minicode.tools._validation import validate_contract


TODO_WRITE_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "todos": {
            "type": "array", "description": "Complete task list. An empty array clears the list.",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "content": {"type": "string", "minLength": 1, "description": "Nonblank task description"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"],
                               "description": "Omit to retain an existing task's status; new tasks default to pending"},
                },
                "required": ["content"],
            },
        },
    },
    "required": ["todos"],
}

# In-memory task storage (resets per session)
_tasks: list[dict[str, Any]] = []
_task_id_counter = 0


def _validate(input_data: dict) -> dict:
    validate_contract(input_data, TODO_WRITE_INPUT)
    todos = input_data["todos"]
    for i, todo in enumerate(todos):
        if not todo["content"].strip():
            raise ValueError(f"$.todos[{i}].content: must be nonblank")
    return {"todos": todos}


def _run(input_data: dict, context) -> ToolResult:
    global _tasks, _task_id_counter

    todos = input_data["todos"]

    existing_by_content = {task["content"]: task for task in _tasks}
    next_tasks = []

    for todo in todos:
        # Try to find existing task by content
        existing = existing_by_content.get(todo["content"])

        if existing:
            # Update existing task
            existing["status"] = todo.get("status", existing["status"])
            if todo.get("status") == "completed" and not existing.get("completed_at"):
                existing["completed_at"] = time.time()
            next_tasks.append(existing)
        else:
            # Create new task
            _task_id_counter += 1
            new_task = {
                "id": _task_id_counter,
                "content": todo["content"],
                "status": todo.get("status", "pending"),
                "created_at": time.time(),
                "completed_at": time.time() if todo.get("status") == "completed" else None,
            }
            existing_by_content[todo["content"]] = new_task
            next_tasks.append(new_task)

    _tasks = next_tasks

    # Format output
    lines = ["Task list updated:", ""]

    for task in _tasks:
        status_icon = {
            "pending": "○",
            "in_progress": "◐",
            "completed": "●",
        }.get(task["status"], "?")

        lines.append(f"{status_icon} [{task['id']}] {task['content']}")

    lines.append("")

    # Summary
    pending = sum(1 for t in _tasks if t["status"] == "pending")
    in_progress = sum(1 for t in _tasks if t["status"] == "in_progress")
    completed = sum(1 for t in _tasks if t["status"] == "completed")
    total = len(_tasks)

    lines.extend([
        f"Total: {total} | Pending: {pending} | In Progress: {in_progress} | Completed: {completed}",
    ])

    return ToolResult(ok=True, output="\n".join(lines))


todo_write_tool = ToolDefinition(
    name="todo_write",
    description="Create or update a list of tasks. Use this to track progress on multi-step tasks. Each task has content (required) and status (pending/in_progress/completed). Pass the complete list each time to update.",
    input_schema=TODO_WRITE_INPUT,
    validator=_validate,
    run=_run,
)
