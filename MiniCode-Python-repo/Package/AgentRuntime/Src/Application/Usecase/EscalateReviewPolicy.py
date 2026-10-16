"""Monotonic review selection from observed files and applied contributions."""
from pathlib import PurePosixPath
import re

from ..Dto.ReviewObservation import ReviewEscalationRequest
from ..Dto.ReviewPolicy import ReviewPolicy


_BEHAVIOR_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".cs", ".php", ".rb", ".sql", ".sh", ".ps1", ".css", ".scss", ".html"}
_CONFIG_FILES = {"package.json", "package-lock.json", "pyproject.toml", "requirements.txt", "cargo.toml",
    "go.mod", "dockerfile", "docker-compose.yml", "compose.yaml", "compose.yml"}
_RISK_PATH = re.compile(r"(?:^|[/_.-])(?:auth|authentication|authorization|security|permissions?|credentials?|"
    r"cryptography|encryption|migrations?|concurrency|threads?|mutex|locks?|deadlock)(?:[/_.-]|$)")
_CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml"}
_SCOPE = {"basic": 0, "focused": 1, "full": 2}


def _behavior(path):
    name = PurePosixPath(path)
    lowered = path.lower()
    if (any(part.lower() in {"test", "tests", "__tests__", "fixtures", "docs", "documentation"} for part in name.parts)
        or re.search(r"(?:^|/)(?:test_|.*\.(?:test|spec)\.)", lowered)):
        return False
    configuration = (name.suffix.lower() in _CONFIG_EXTENSIONS and (
        any(part.lower() in {"config", "configuration", "settings"} for part in name.parts[:-1])
        or _RISK_PATH.search(lowered)
        or re.search(r"(?:^|[._-])(?:config|settings)(?:[._-]|$)", name.name.lower())))
    return name.suffix.lower() in _BEHAVIOR_EXTENSIONS or name.name.lower() in _CONFIG_FILES or bool(configuration)


def _module(path):
    parts = PurePosixPath(path).parts
    for index, part in enumerate(parts[:-1]):
        if part in {"Main", "Package"} and index + 1 < len(parts) - 1:
            return "/".join(parts[:index + 2])
        if part.lower() in {"features", "modules", "services", "packages"} and index + 1 < len(parts) - 1:
            return "/".join(parts[:index + 2])
        if part.lower() in {"config", "configuration", "settings"}:
            return "/".join((*parts[:index + 1], "project-config"))
    if parts[-1].lower() in _CONFIG_FILES:
        return "/".join((*parts[:-1], "project-config"))
    # Source and adjacent styles of one component share an implementation unit.
    return str(PurePosixPath(path).with_suffix(""))


class EscalateReviewPolicy:
    def execute(self, request: ReviewEscalationRequest) -> ReviewPolicy:
        admitted, current = request.admitted, request.current
        if (_SCOPE[current.scope] < _SCOPE[admitted.scope] or admitted.required and not current.required
            or type(request.max_agents) is not int or request.max_agents < 1):
            raise ValueError("review escalation cannot lower admission requirements")
        if "independent review explicitly disabled" in admitted.reasons:
            return admitted
        changes = tuple(path for path in request.observation.changed_resources if _behavior(path))
        risk = any(_RISK_PATH.search(path.lower()) or PurePosixPath(path).suffix.lower() == ".sql" for path in changes)
        crossed = len({_module(path) for path in changes}) > 1
        integrated = bool(changes and request.observation.integrated_nodes)
        scope = "full" if risk or crossed or integrated else "focused" if changes else "basic"
        required = risk or crossed or integrated
        if not required and request.max_agents < 2:
            return current
        if _SCOPE[scope] < _SCOPE[current.scope] or scope == current.scope and (current.required or not required):
            return current
        reasons = tuple(dict.fromkeys((*current.reasons,
            *( ("observed security, migration or concurrency-sensitive source change",) if risk else ()),
            *( ("observed changes span behavior modules",) if crossed else ()),
            *( ("observed applied agent contributions require integration review",) if integrated else ()),
            *( ("observed bounded behavior change",) if scope == "focused" else ()))))
        return ReviewPolicy(scope, current.required or required, reasons,
            reasoning_budget=request.reasoning_effort or ("xhigh" if scope == "full" else "medium"),
            runtime_profile="single-deep" if scope == "full" else "single")
