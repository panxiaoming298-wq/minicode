"""Capability Registry - Self-describing tool registration system.

Inspired by Skill architecture: each capability self-describes, self-registers,
has dependency graph. Tools are not isolated functions but triggerable,
readable, extensible capability units.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from minicode.logging_config import get_logger

logger = get_logger("capability_registry")


class CapabilityDomain(str, Enum):
    FILE = "file"
    CODE = "code"
    SEARCH = "search"
    WEB = "web"
    SYSTEM = "system"
    MEMORY = "memory"
    COMMUNICATION = "communication"
    ANALYSIS = "analysis"
    EXECUTION = "execution"
    UNKNOWN = "unknown"


class CapabilityScope(str, Enum):
    READONLY = "readonly"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


@dataclass
class CapabilityMetadata:
    name: str
    domain: CapabilityDomain
    scope: CapabilityScope
    description: str
    version: str = "1.0.0"
    author: str = ""
    dependencies: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "domain": self.domain.value,
            "scope": self.scope.value, "description": self.description,
            "version": self.version, "author": self.author,
            "dependencies": self.dependencies,
            "required_permissions": self.required_permissions,
            "examples": self.examples, "tags": self.tags,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@runtime_checkable
class Capability(Protocol):
    @property
    def metadata(self) -> CapabilityMetadata: ...
    def execute(self, params: dict[str, Any]) -> dict[str, Any]: ...
    def validate(self, params: dict[str, Any]) -> tuple[bool, str]: ...


@dataclass
class RegisteredCapability:
    metadata: CapabilityMetadata
    handler: Callable[..., Any]
    validator: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    instance: Any | None = None
    call_count: int = 0
    total_execution_time: float = 0.0
    last_used: float = 0.0

    def execute(self, params: dict[str, Any]) -> Any:
        start = time.time()
        self.call_count += 1
        self.last_used = start
        try:
            if self.instance is not None:
                result = self.handler(self.instance, **params)
            else:
                result = self.handler(**params)
            self.total_execution_time += time.time() - start
            return result
        except Exception:
            self.total_execution_time += time.time() - start
            raise

    def validate(self, params: dict[str, Any]) -> tuple[bool, str]:
        if self.validator:
            return self.validator(params)
        return True, ""

    @property
    def avg_execution_time(self) -> float:
        return self.total_execution_time / self.call_count if self.call_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "call_count": self.call_count,
            "avg_execution_time_ms": round(self.avg_execution_time * 1000, 2),
            "last_used": self.last_used,
        }


class CapabilityRegistry:
    def __init__(self):
        self._capabilities: dict[str, RegisteredCapability] = {}
        self._domain_index: dict[CapabilityDomain, set[str]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._dependency_graph: dict[str, set[str]] = {}

    def register(self, metadata: CapabilityMetadata, handler: Callable[..., Any],
                 validator: Callable | None = None, instance: Any | None = None) -> RegisteredCapability:
        name = metadata.name
        if name in self._capabilities:
            logger.warning("Capability '%s' already registered, updating", name)

        cap = RegisteredCapability(metadata=metadata, handler=handler, validator=validator, instance=instance)
        self._capabilities[name] = cap

        domain = metadata.domain
        if domain not in self._domain_index:
            self._domain_index[domain] = set()
        self._domain_index[domain].add(name)

        for tag in metadata.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = set()
            self._tag_index[tag].add(name)

        self._dependency_graph[name] = set(metadata.dependencies)
        logger.debug("Registered capability: %s (%s)", name, domain.value)
        return cap

    def unregister(self, name: str) -> bool:
        if name not in self._capabilities:
            return False
        cap = self._capabilities.pop(name)
        self._domain_index.get(cap.metadata.domain, set()).discard(name)
        for tag in cap.metadata.tags:
            self._tag_index.get(tag, set()).discard(name)
        self._dependency_graph.pop(name, None)
        logger.debug("Unregistered capability: %s", name)
        return True

    def get(self, name: str) -> RegisteredCapability | None:
        return self._capabilities.get(name)

    def has(self, name: str) -> bool:
        return name in self._capabilities

    def list_all(self) -> list[str]:
        return list(self._capabilities.keys())

    def list_by_domain(self, domain: CapabilityDomain) -> list[str]:
        return list(self._domain_index.get(domain, set()))

    def list_by_tag(self, tag: str) -> list[str]:
        return list(self._tag_index.get(tag, set()))

    def search(self, query: str) -> list[tuple[str, float]]:
        query_lower = query.lower()
        results: list[tuple[str, float]] = []
        for name, cap in self._capabilities.items():
            score = 0.0
            if query_lower in name.lower():
                score += 1.0
            if query_lower in cap.metadata.description.lower():
                score += 0.5
            for tag in cap.metadata.tags:
                if query_lower in tag.lower():
                    score += 0.3
            if query_lower in cap.metadata.domain.value.lower():
                score += 0.2
            if score > 0:
                results.append((name, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def get_dependencies(self, name: str) -> set[str]:
        return self._dependency_graph.get(name, set()).copy()

    def get_all_dependencies(self, name: str) -> set[str]:
        visited: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for dep in self._dependency_graph.get(current, set()):
                if dep not in visited:
                    stack.append(dep)
        visited.discard(name)
        return visited

    def check_dependencies(self, name: str) -> tuple[bool, list[str]]:
        deps = self._dependency_graph.get(name, set())
        missing = [d for d in deps if d not in self._capabilities]
        return len(missing) == 0, missing

    def get_stats(self) -> dict[str, Any]:
        return {
            "total_capabilities": len(self._capabilities),
            "domains": {domain.value: len(caps) for domain, caps in self._domain_index.items()},
            "tags": {tag: len(caps) for tag, caps in self._tag_index.items()},
            "most_used": sorted(
                [(name, cap.call_count) for name, cap in self._capabilities.items()],
                key=lambda x: x[1], reverse=True,
            )[:10],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": {name: cap.to_dict() for name, cap in self._capabilities.items()},
            "stats": self.get_stats(),
        }


_registry: CapabilityRegistry | None = None


def get_registry() -> CapabilityRegistry:
    global _registry
    if _registry is None:
        _registry = CapabilityRegistry()
    return _registry


def capability(name: str, domain: CapabilityDomain, scope: CapabilityScope,
               description: str, version: str = "1.0.0",
               dependencies: list[str] | None = None,
               permissions: list[str] | None = None,
               tags: list[str] | None = None,
               examples: list[str] | None = None):
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        metadata = CapabilityMetadata(
            name=name, domain=domain, scope=scope, description=description,
            version=version, dependencies=dependencies or [],
            required_permissions=permissions or [], tags=tags or [], examples=examples or [],
        )
        sig = inspect.signature(func)
        def validator(params: dict[str, Any]) -> tuple[bool, str]:
            for param_name, param in sig.parameters.items():
                if param_name == "self":
                    continue
                if param.default is inspect.Parameter.empty and param_name not in params:
                    return False, f"Missing required parameter: {param_name}"
            return True, ""
        get_registry().register(metadata, func, validator)
        return func
    return decorator


def register_instance_capability(instance: Any, method_name: str,
                                 metadata: CapabilityMetadata) -> RegisteredCapability:
    handler = getattr(instance, method_name)
    return get_registry().register(metadata, handler, instance=instance)
