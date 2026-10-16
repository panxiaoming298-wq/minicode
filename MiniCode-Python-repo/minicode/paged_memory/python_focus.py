"""Conservative one-hop Python bindings, without imports or a repository index."""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import PurePosixPath


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def scoped_nodes(node):
    """Visit a function's own scope, not deferred nested function bodies."""
    yield node
    if isinstance(node, (*FUNCTIONS, ast.ClassDef, ast.Lambda)):
        return
    for child in ast.iter_child_nodes(node):
        yield from scoped_nodes(child)


@dataclass
class PythonSource:
    path: str
    text: str
    digest: str
    event_ids: list[str]
    tree: ast.Module = field(init=False)
    symbols: dict[str, list[ast.AST]] = field(init=False, default_factory=dict)
    bindings: dict[str, list[tuple]] = field(init=False, default_factory=dict)
    wildcard: bool = field(init=False, default=False)

    def __post_init__(self):
        self.tree = ast.parse(self.text)
        pending, count = [(self.tree, 0)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 20000 or depth > 100:
                raise ValueError("Python focus syntax analysis limit")
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))

        def definitions(body, prefix=""):
            for node in body:
                if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
                    name = prefix + node.name
                    if isinstance(node, FUNCTIONS):
                        self.symbols.setdefault(name, []).append(node)
                    definitions(node.body, name + ".")
        definitions(self.tree.body)
        # Conditional definitions, reassignment and duplicate imports are not
        # resolved optimistically: order and runtime branches matter.
        for top in self.tree.body:
            for node in scoped_nodes(top):
                if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
                    binding = ("definition", node.name) if node is top else ("unknown",)
                    self.bindings.setdefault(node.name, []).append(binding)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.bindings.setdefault(alias.asname or alias.name.split(".")[0], []).append(
                            ("module", alias.name if alias.asname else alias.name.split(".")[0], 0)
                            if node is top else ("unknown",))
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "*":
                            self.wildcard = True
                        else:
                            self.bindings.setdefault(alias.asname or alias.name, []).append(
                                ("from", node.module or "", node.level, alias.name) if node is top else ("unknown",))
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    self.bindings.setdefault(node.id, []).append(("unknown",))
                elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                    self.bindings.setdefault(node.name, []).append(("unknown",))
                elif isinstance(node, ast.MatchMapping) and node.rest:
                    self.bindings.setdefault(node.rest, []).append(("unknown",))
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    root = node.value
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name):
                        self.bindings.setdefault(root.id, []).append(("unknown",))

    def node(self, symbol):
        nodes = self.symbols.get(symbol, [])
        return nodes[0] if len(nodes) == 1 else None

    def span(self, symbol):
        node = self.node(symbol)
        if node is None:
            return None
        return min([node.lineno, *(d.lineno for d in node.decorator_list)]), node.end_lineno

    def body(self, symbol):
        first, last = self.span(symbol)
        return "".join(self.text.splitlines(keepends=True)[first - 1:last])

    def imports_for(self, symbol):
        names = {node.id for statement in self.node(symbol).body for node in scoped_nodes(statement)
                 if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        lines = self.text.splitlines(keepends=True)
        imports = []
        for node in self.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.split(".")[0]) in names for alias in node.names
            ):
                imports.append("".join(lines[node.lineno - 1:node.end_lineno]))
        return "".join(imports)


def module_paths(source_path, module, level):
    parts = module.split(".") if module else []
    if level:
        parent = PurePosixPath(source_path).parent
        if level > len(parent.parts):
            return []
        for _ in range(level - 1):
            parent = parent.parent
        base = parent.joinpath(*parts)
    else:
        base = PurePosixPath(*parts)
    return [str(base) + ".py", (base / "__init__.py").as_posix()]


def direct_dependencies(source: PythonSource, symbol: str, sources: dict[str, PythonSource], *, module_exists=None):
    """Return proven static declarations and explicit unresolved call sites.

    Runtime monkey-patching, descriptors and dynamic dispatch are outside this
    contract. Nested scopes and local names never bind to a same-named global.
    """
    target = source.node(symbol)
    if target is None:
        return [], [{"call": symbol, "reason": "ambiguous_symbol"}]
    if any(isinstance(source.node(".".join(symbol.split(".")[:i])), FUNCTIONS)
           for i in range(1, len(symbol.split(".")))):
        return [], [{"call": symbol, "reason": "nested_closure_scope"}]
    nodes = [node for statement in target.body for node in scoped_nodes(statement)]
    local = {arg.arg for arg in [*target.args.posonlyargs, *target.args.args, *target.args.kwonlyargs]}
    local.update(arg.arg for arg in (target.args.vararg, target.args.kwarg) if arg)
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            local.add(node.id)
        elif isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            local.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            local.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            local.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            local.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            local.add(node.rest)
    resolved, pending, seen = [], [], set()
    calls = [node for node in nodes if isinstance(node, ast.Call)]
    for call in calls[:32]:
        label = ast.unparse(call.func)[:160]
        attrs, root = [], call.func
        while isinstance(root, ast.Attribute):
            attrs.insert(0, root.attr)
            root = root.value
        reason, paths, name = "dynamic_call", [], ""
        if isinstance(root, ast.Name):
            bindings = source.bindings.get(root.id, [])
            if root.id in local:
                reason = "local_or_parameter_binding"
            elif source.wildcard or len(bindings) > 1:
                reason = "ambiguous_binding"
            elif not bindings:
                if not attrs and root.id in vars(builtins):
                    continue
                reason = "unknown_binding"
            else:
                binding = bindings[0]
                if binding[0] == "definition" and not attrs:
                    paths, name = [source.path], binding[1]
                elif binding[0] == "module" and attrs:
                    paths = module_paths(source.path, ".".join([binding[1], *attrs[:-1]]), 0)
                    name = attrs[-1]
                elif binding[0] == "from":
                    module = binding[1]
                    if attrs:
                        module = ".".join(p for p in [module, binding[3], *attrs[:-1]] if p)
                    paths = module_paths(source.path, module, binding[2])
                    name = attrs[-1] if attrs else binding[3]
                else:
                    reason = "unsupported_binding"
        if paths:
            possible = [path for path in paths if module_exists(path)] if module_exists else [path for path in paths if path in sources]
            available = [sources[path] for path in possible if path in sources]
            if len(possible) == len(available) == 1:
                dependency = available[0]
                bound = dependency.bindings.get(name, [])
                if dependency.node(name) is not None and bound == [("definition", name)] and not dependency.wildcard:
                    key = dependency.path, name
                    if key != (source.path, symbol) and key not in seen:
                        resolved.append(key)
                        seen.add(key)
                    continue
                reason = "ambiguous_or_reexported_symbol"
            else:
                reason = "source_not_read_or_ambiguous_module"
        entry = {"call": label, "reason": reason, "line": call.lineno}
        if paths:
            entry["candidate_paths"] = paths
        if entry not in pending:
            pending.append(entry)
    if len(calls) > 32:
        pending.append({"reason": "call_limit", "omitted_calls": len(calls) - 32})
    return resolved, pending
