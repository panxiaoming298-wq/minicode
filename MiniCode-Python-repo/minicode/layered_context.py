"""Layered Context - Hierarchical context architecture.

Inspired by information layering:
- System: global rules, core capabilities
- Project: project memory, conventions, architecture
- Session: conversation history, working memory
- Scratchpad: current round drafts, intermediate results
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("layered_context")


class ContextLayer(str, Enum):
    SYSTEM = "system"
    PROJECT = "project"
    SESSION = "session"
    SCRATCHPAD = "scratchpad"


@dataclass
class LayerContent:
    text: str = ""
    tokens: int = 0
    priority: int = 0
    timestamp: float = field(default_factory=time.time)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "tokens": self.tokens,
                "priority": self.priority, "timestamp": self.timestamp, "source": self.source}


@dataclass
class ContextBudget:
    total_limit: int = 8000
    system_ratio: float = 0.15
    project_ratio: float = 0.25
    session_ratio: float = 0.45
    scratchpad_ratio: float = 0.15

    @property
    def system_limit(self) -> int:
        return int(self.total_limit * self.system_ratio)

    @property
    def project_limit(self) -> int:
        return int(self.total_limit * self.project_ratio)

    @property
    def session_limit(self) -> int:
        return int(self.total_limit * self.session_ratio)

    @property
    def scratchpad_limit(self) -> int:
        return int(self.total_limit * self.scratchpad_ratio)

    def get_limit(self, layer: ContextLayer) -> int:
        return {ContextLayer.SYSTEM: self.system_limit,
                ContextLayer.PROJECT: self.project_limit,
                ContextLayer.SESSION: self.session_limit,
                ContextLayer.SCRATCHPAD: self.scratchpad_limit}.get(layer, 0)


class LayeredContext:
    def __init__(self, budget: ContextBudget | None = None):
        self.budget = budget or ContextBudget()
        self._layers: dict[ContextLayer, list[LayerContent]] = {
            ContextLayer.SYSTEM: [], ContextLayer.PROJECT: [],
            ContextLayer.SESSION: [], ContextLayer.SCRATCHPAD: [],
        }
        self._layer_tokens: dict[ContextLayer, int] = {
            ContextLayer.SYSTEM: 0, ContextLayer.PROJECT: 0,
            ContextLayer.SESSION: 0, ContextLayer.SCRATCHPAD: 0,
        }

    def add(self, layer: ContextLayer, text: str, tokens: int | None = None,
            priority: int = 0, source: str = "") -> None:
        if tokens is None:
            tokens = self._estimate_tokens(text)
        content = LayerContent(text=text, tokens=tokens, priority=priority, source=source)
        self._layers[layer].append(content)
        self._layer_tokens[layer] += tokens
        self._trim_layer(layer)

    def set(self, layer: ContextLayer, contents: list[LayerContent]) -> None:
        self._layers[layer] = contents
        self._layer_tokens[layer] = sum(c.tokens for c in contents)
        self._trim_layer(layer)

    def clear(self, layer: ContextLayer) -> None:
        self._layers[layer].clear()
        self._layer_tokens[layer] = 0

    def clear_scratchpad(self) -> None:
        self.clear(ContextLayer.SCRATCHPAD)

    def get(self, layer: ContextLayer) -> list[LayerContent]:
        return list(self._layers[layer])

    def get_text(self, layer: ContextLayer) -> str:
        return "\n\n".join(c.text for c in self._layers[layer] if c.text)

    def get_all_text(self) -> str:
        parts: list[str] = []
        for layer in ContextLayer:
            text = self.get_text(layer)
            if text:
                parts.append(f"<!-- {layer.value} -->\n{text}")
        return "\n\n".join(parts)

    def get_total_tokens(self) -> int:
        return sum(self._layer_tokens.values())

    def get_layer_tokens(self, layer: ContextLayer) -> int:
        return self._layer_tokens[layer]

    def _trim_layer(self, layer: ContextLayer) -> None:
        limit = self.budget.get_limit(layer)
        contents = self._layers[layer]
        if sum(c.tokens for c in contents) <= limit:
            return
        sorted_contents = sorted(contents, key=lambda c: (-c.priority, c.timestamp))
        kept: list[LayerContent] = []
        total = 0
        for content in sorted_contents:
            if total + content.tokens <= limit:
                kept.append(content)
                total += content.tokens
            elif not kept:
                # First item exceeds budget: keep truncated version
                content.tokens = limit
                kept.append(content)
                total = limit
                break
        kept_ids = {id(c) for c in kept}
        self._layers[layer] = [c for c in contents if id(c) in kept_ids]
        self._layer_tokens[layer] = total
        removed = len(contents) - len(kept)
        if removed > 0:
            logger.debug("Trimmed %d items from %s layer", removed, layer.value)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count: ~4 chars/token (ASCII), ~1.5 chars/token (CJK).

        Consistent with ContextManager's estimate_message_tokens formula.
        """
        cjk = sum(1 for ch in text if '一' <= ch <= '鿿' or '㐀' <= ch <= '䶿')
        ascii_chars = len(text) - cjk
        return max(1, int(ascii_chars / 4.0 + cjk / 1.5))

    def optimize(self) -> dict[str, Any]:
        stats = {"before_tokens": self.get_total_tokens(), "removed_items": 0, "merged_items": 0}
        for layer in ContextLayer:
            contents = self._layers[layer]
            non_empty = [c for c in contents if c.text.strip()]
            stats["removed_items"] += len(contents) - len(non_empty)
            merged: list[LayerContent] = []
            for content in non_empty:
                if merged and merged[-1].source == content.source:
                    merged[-1].text += "\n" + content.text
                    merged[-1].tokens += content.tokens
                    stats["merged_items"] += 1
                else:
                    merged.append(content)
            self._layers[layer] = merged
            self._layer_tokens[layer] = sum(c.tokens for c in merged)
        stats["after_tokens"] = self.get_total_tokens()
        stats["saved_tokens"] = stats["before_tokens"] - stats["after_tokens"]
        return stats

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": {"total_limit": self.budget.total_limit,
                       "system_limit": self.budget.system_limit,
                       "project_limit": self.budget.project_limit,
                       "session_limit": self.budget.session_limit,
                       "scratchpad_limit": self.budget.scratchpad_limit},
            "layers": {layer.value: {"items": len(contents),
                                      "tokens": self._layer_tokens[layer],
                                      "contents": [c.to_dict() for c in contents]}
                       for layer, contents in self._layers.items()},
            "total_tokens": self.get_total_tokens(),
        }


class ContextBuilder:
    def __init__(self, layered_context: LayeredContext | None = None):
        self.context = layered_context or LayeredContext()

    def set_system_prompt(self, prompt: str, tokens: int | None = None) -> None:
        self.context.clear(ContextLayer.SYSTEM)
        self.context.add(ContextLayer.SYSTEM, prompt, tokens, priority=100, source="system_prompt")

    def add_project_memory(self, memory_text: str, tokens: int | None = None) -> None:
        self.context.add(ContextLayer.PROJECT, memory_text, tokens, priority=80, source="project_memory")

    def add_session_message(self, role: str, content: str, tokens: int | None = None) -> None:
        text = f"{role}: {content}"
        self.context.add(ContextLayer.SESSION, text, tokens, priority=50, source=f"msg_{role}")

    def add_scratchpad(self, content: str, tokens: int | None = None) -> None:
        self.context.add(ContextLayer.SCRATCHPAD, content, tokens, priority=10, source="scratchpad")

    def build(self) -> str:
        return self.context.get_all_text()

    def get_token_stats(self) -> dict[str, int]:
        return {layer.value: self.context.get_layer_tokens(layer) for layer in ContextLayer}
