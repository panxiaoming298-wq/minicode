"""Private observation seam for isolated runs of the real Web worker.

Not a Web request option. The evaluation launcher owns isolation and transports
the config-loader snapshot over stdin, never via arguments or report files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class WebRunProbe:
    runtime_snapshot: dict[str, Any] = field(repr=False)
    allow_generic_delegation: bool = True
    prepare_only: bool = False
    # Reach the real Runtime assembly with zero model requests before reporting
    # preparation, so a capability receipt describes objects that were built.
    assemble_only: bool = False
    on_prepared: Callable[..., None] | None = field(default=None, repr=False)
    approval_prompt: Callable[[dict[str, Any]], dict[str, Any]] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.allow_generic_delegation) is not bool or type(self.prepare_only) is not bool:
            raise ValueError("probe capability and preparation flags must be booleans")
        if type(self.assemble_only) is not bool:
            raise ValueError("probe assembly flag must be a boolean")
        if self.assemble_only and not self.prepare_only:
            raise ValueError("assembly preparation is only meaningful for a prepare-only probe")
