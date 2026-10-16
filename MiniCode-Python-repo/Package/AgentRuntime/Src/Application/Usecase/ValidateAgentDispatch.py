"""Validate dispatch data without granting execution or tool authority."""

from collections.abc import Mapping
from pathlib import PureWindowsPath
import re

from ..Dto.AgentDispatch import AgentDispatch


class ValidateAgentDispatch:
    def execute(self, value: Mapping) -> tuple[AgentDispatch, ...]:
        if not isinstance(value, Mapping) or set(value) != {"tasks"}:
            raise ValueError("dispatch requires only a tasks array")
        tasks = value["tasks"]
        if not isinstance(tasks, (list, tuple)) or not 1 <= len(tasks) <= 8:
            raise ValueError("tasks must contain 1..8 agent assignments")
        result = []
        keys = set()
        for task in tasks:
            if not isinstance(task, Mapping) or set(task) - {"idempotency_key", "objective", "agent_type", "context_mode", "isolation", "report_artifact_ids", "write_scope", "name", "acceptance_criteria", "checks", "optional"}:
                raise ValueError("unknown agent assignment fields")
            key, objective = task.get("idempotency_key"), task.get("objective")
            if not isinstance(key, str) or not 1 <= len(key) <= 128 or key != key.strip():
                raise ValueError("idempotency_key must be a stable 1..128 character key")
            if not isinstance(objective, str) or not objective.strip() or len(objective) > 12000:
                raise ValueError("objective must contain 1..12000 characters")
            role = task.get("agent_type", "explore")
            if role not in {"explore", "plan", "review", "verify", "worker"}:
                raise ValueError("only explore, plan, review, verify and worker agents are supported")
            optional = task.get("optional", False)
            if type(optional) is not bool or (optional and role not in {"explore", "plan", "review"}):
                raise ValueError("optional must be boolean and is only available to explore, plan and review assistants")
            context_mode = task.get("context_mode", "isolated")
            if context_mode not in {"isolated", "fork"} or (context_mode == "fork" and role == "verify"):
                raise ValueError("context_mode must be isolated or fork; independent verification cannot fork implementation history")
            isolation = task.get("isolation", "snapshot" if role == "verify" else "shared")
            if not isinstance(isolation, str) or isolation not in {"shared", "snapshot", "worktree"}:
                raise ValueError("isolation must be shared, snapshot or worktree")
            if role == "verify" and isolation == "shared":
                raise ValueError("independent verification requires a fixed snapshot or worktree; use review for live shared analysis")
            scope = task.get("write_scope", ())
            if not isinstance(scope, (list, tuple)) or len(scope) > 64:
                raise ValueError("write_scope must contain at most 64 relative file or directory paths")
            for path in scope:
                if (not isinstance(path, str) or not path or path != path.strip()
                    or path.startswith("/") or any(c in path for c in "\\:*?<>|\"") or any(ord(c) < 32 for c in path)
                    or any(PureWindowsPath(p).is_reserved() for p in path.split("/"))
                    or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in path.split("/"))
                    or any(p.lower() == ".git" for p in path.split("/"))):
                    raise ValueError("write_scope must use bounded relative paths without aliases or wildcards")
            if role == "worker":
                if not scope or isolation != "shared" or context_mode != "isolated":
                    raise ValueError("worker requires nonempty write_scope, isolation=shared and independent context")
            elif scope:
                raise ValueError("write_scope is only available to worker agents")
            if key in keys:
                raise ValueError("duplicate idempotency_key in one batch")
            keys.add(key)
            reports = task.get("report_artifact_ids", ())
            if (not isinstance(reports, (list, tuple)) or len(reports) > 8
                or any(not isinstance(item, str) or not re.fullmatch(r"artifact-[a-f0-9]{32}", item) for item in reports)
                or len(set(reports)) != len(reports)):
                raise ValueError("report_artifact_ids must contain at most 8 unique Runtime artifact IDs")
            name = task.get("name", "")
            if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name):
                raise ValueError("name must be at most 80 characters without control characters")
            criteria = task.get("acceptance_criteria", ())
            if (not isinstance(criteria, (list, tuple)) or len(criteria) > 16
                or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in criteria)):
                raise ValueError("acceptance_criteria must contain at most 16 nonempty bounded descriptions")
            checks = self.validate_checks(task.get("checks", ()))
            if checks and role != "worker":
                raise ValueError("checks require a scoped worker assignment")
            result.append(AgentDispatch(key, objective.strip(), role, context_mode, isolation, tuple(reports),
                tuple(sorted(set(scope))), name.strip(), tuple(item.strip() for item in criteria), checks, optional))
        return tuple(result)

    @staticmethod
    def validate_checks(checks):
        if not isinstance(checks, (list, tuple)) or len(checks) > 8:
            raise ValueError("checks must contain at most 8 declared local commands")
        result, ids = [], set()
        for check in checks:
            if not isinstance(check, Mapping) or set(check) - {"id", "command", "args", "cwd", "timeout_seconds"}:
                raise ValueError("unknown local check fields")
            key, command = check.get("id"), check.get("command")
            args, cwd, timeout = check.get("args", []), check.get("cwd", "."), check.get("timeout_seconds", 120)
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or key in ids:
                raise ValueError("check id must be a unique bounded identifier")
            if not isinstance(command, str) or not command.strip() or len(command) > 1024 or any(c in command for c in "\n\r\x00"):
                raise ValueError("check command must name one executable")
            if (not isinstance(args, (list, tuple)) or len(args) > 64
                or any(not isinstance(arg, str) or len(arg) > 4096 or "\x00" in arg for arg in args)):
                raise ValueError("check args must be a bounded argument array")
            if (not isinstance(cwd, str) or not cwd or cwd.startswith("/") or any(c in cwd for c in "\\:*?<>|\"")
                or (cwd != "." and any(p in {"", ".", ".."} or p.lower() == ".git" for p in cwd.split("/")))):
                raise ValueError("check cwd must stay inside the copied workspace")
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ValueError("check timeout_seconds must be between 1 and 300")
            result.append({"id": key, "command": command, "args": list(args), "cwd": cwd, "timeout_seconds": timeout})
            ids.add(key)
        return tuple(result)
