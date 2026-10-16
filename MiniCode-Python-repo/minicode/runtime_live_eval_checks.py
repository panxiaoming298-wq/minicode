"""Immutable, stdlib-only acceptance checks for the live Runtime pilot.

Run by absolute path with ``python -I -B``. Never imports a candidate module.
The pilot deliberately accepts a small pure-Python subset, not arbitrary repo
programs: no imports, attributes, decorators, indirect calls or executable
module statements. This restriction is part of both arms' published contract;
it is not a general-purpose Python/OS sandbox.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys


SAFE_BUILTINS = {
    "len": len, "min": min, "max": max, "range": range,
    "isinstance": isinstance, "int": int, "bool": bool,
    "ValueError": ValueError,
}
ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
    ast.If, ast.Assign, ast.AugAssign, ast.Raise, ast.Expr,
    ast.Name, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.IfExp, ast.Call, ast.keyword,
    ast.Load, ast.Store, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
)


def load_pure_function(source: str, function_name: str):
    if len(source.encode("utf-8")) > 20_000:
        raise ValueError("candidate exceeds the published 20 KB source limit")
    tree = ast.parse(source)
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(definitions) != 1 or definitions[0].name != function_name:
        raise ValueError(f"define only the function {function_name}")
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) and not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            raise ValueError("only a function definition and docstrings are allowed at module scope")
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(f"pure-function contract disallows {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("dunder names are not allowed")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in SAFE_BUILTINS
        ):
            raise ValueError("only the documented built-in calls are allowed")
        if isinstance(node, ast.FunctionDef) and (
            node is not definitions[0] or node.decorator_list or node.returns
            or node.args.defaults or any(node.args.kw_defaults)
            or any(arg.annotation for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
            or node.args.vararg or node.args.kwarg
        ):
            raise ValueError("nested functions, annotations, defaults and decorators are not allowed")
    scope = {"__builtins__": dict(SAFE_BUILTINS)}
    exec(compile(tree, "<pilot-candidate>", "exec"), scope, scope)
    return scope[function_name]


def check_workspace(workspace: Path) -> dict:
    source_path = workspace / "pagination.py"
    if source_path.is_symlink() or source_path.resolve().parent != workspace.resolve():
        raise ValueError("candidate must be a direct regular workspace file")
    function = load_pure_function(source_path.read_text(encoding="utf-8"), "paginate")
    failures: list[str] = []
    checks = 0
    # Expected results are calculated here, outside the candidate workspace.
    for length in (0, 1, 2, 5, 11):
        for size in (1, 2, 4, 20):
            for page in (1, 2, 3, 12):
                values = list(range(length))
                original = values[:]
                expected = values[(page - 1) * size:page * size]
                checks += 1
                try:
                    result = function(values, page, size)
                    if result != expected or values != original or result is values:
                        failures.append(f"length={length}, page={page}, size={size}: wrong slice, mutation or alias")
                except Exception as error:
                    failures.append(f"valid input raised {type(error).__name__}")
    for page, size in ((0, 1), (-1, 2), (1, 0), (1, -1), (True, 2), (1, False), (1.5, 2), (1, "2")):
        checks += 1
        try:
            function([1, 2, 3], page, size)
        except ValueError:
            continue
        except Exception as error:
            failures.append(f"invalid input raised {type(error).__name__}, expected ValueError")
        else:
            failures.append("invalid page/page_size did not raise ValueError")
    return {"passed": not failures, "checks": checks, "failures": failures[:12]}


def main() -> int:
    try:
        result = check_workspace(Path(sys.argv[1]).resolve())
    except Exception as error:
        result = {"passed": False, "checks": 0, "failures": [f"{type(error).__name__}: {error}"]}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
