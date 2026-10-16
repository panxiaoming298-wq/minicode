"""Provider-neutral inputs and outcome for failure disposition policy."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailureDiagnosisRequest:
    state: str
    kind: str
    role: str
    effects: tuple[str, ...]
    criticality: str = "required"
    consultation: bool = False
    ambiguous_side_effect: bool = False
    failed_verification: bool = False


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    category: str
    fallback_allowed: bool
