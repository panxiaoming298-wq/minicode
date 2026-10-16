"""Trusted human text used to resolve an independent-review preference."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewDirectiveRequest:
    text: str
    mode: str
    # History contributes conversation preferences; only the current request
    # can contribute a single-run override.
    include_run_scoped: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("review directive text must be a string")
        if self.mode not in {"standard", "deep", "ultra"}:
            raise ValueError("invalid review directive mode")
        if type(self.include_run_scoped) is not bool:
            raise ValueError("include_run_scoped must be a boolean")
