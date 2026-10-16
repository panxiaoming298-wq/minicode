from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict


class ChatMessage(TypedDict, total=False):
    role: Literal[
        "system",
        "user",
        "assistant",
        "assistant_progress",
        "assistant_tool_call",
        "tool_result",
    ]
    content: str
    reasoningContent: str
    toolUseId: str
    toolName: str
    input: Any
    isError: bool
    imageInputIds: list[str]
    inputError: str
    rawArguments: str
    _runtimeMessageKind: Literal["control", "feedback"]
    _runtimeFeedbackKind: Literal["continuation"]


class ToolCall(TypedDict):
    id: str
    toolName: str
    input: Any
    inputError: NotRequired[str]
    rawArguments: NotRequired[str]


@dataclass(slots=True)
class StepDiagnostics:
    stopReason: str | None = None
    blockTypes: list[str] = field(default_factory=list)
    ignoredBlockTypes: list[str] = field(default_factory=list)
    # Transport integrity is separate from the provider's own finish reason.
    # A missing terminal marker means "unknown remainder", not "provider stopped".
    transportComplete: bool = True
    transportTermination: str = "unknown"
    rawResponseIncompleteReason: str | None = None
    # Network end, SSE/body parse integrity and tool-call assembly are three
    # different facts. ``transportComplete`` is the conjunction that guards
    # execution; these fields keep each fact separately auditable so a complete
    # transport with a lost event cannot be reported as a trustworthy batch.
    networkComplete: bool = True
    parseComplete: bool = True
    assemblyComplete: bool = True
    lostEventCount: int = 0
    streamLossReasons: list[str] = field(default_factory=list)
    argumentDiagnostics: list[dict[str, Any]] = field(default_factory=list)
    # Exact request policy after admission, not the value displayed in settings.
    outputBudget: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentStep:
    type: Literal["assistant", "tool_calls"]
    content: str = ""
    kind: Literal["final", "progress"] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    contentKind: Literal["final", "progress"] | None = None
    reasoningContent: str = ""
    diagnostics: StepDiagnostics | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    # Actual managed pixels sent with this successful provider response.
    # Set by native adapters, never inferred from generated text.
    imageInputIds: list[str] = field(default_factory=list)


RuntimeEventCategory = Literal[
    "phase",
    "compaction",
    "guard",
    "widening",
    "recovery",
    "stop",
]


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    category: RuntimeEventCategory
    message: str
    step: int | None = None
    profile: str = ""
    phase: str = ""
    verification_focus: str = ""
    stop_reason: str = ""
    widening_reason: str = ""
    evidence_summary: str = ""


class ModelAdapter(Protocol):
    def next(
        self,
        messages: list[ChatMessage],
        on_stream_chunk: Callable[[str], None] | None = None,
        store: Any | None = None,
    ) -> AgentStep: ...
