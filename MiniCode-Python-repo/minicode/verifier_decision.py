"""Shared syntax for a verifier's decision, not proof that its claims are true."""
import re


def terminal_marker(output: str) -> str | None:
    nonempty = [line.strip() for line in output.splitlines() if line.strip()]
    markers = re.findall(r"<verification_(?:passed|failed|unavailable)>", output)
    return markers[0] if len(markers) == 1 and nonempty and nonempty[-1] == markers[0] else None


def parse_verifier_decision(output: str) -> tuple[bool, str]:
    nonempty = [line.strip() for line in output.splitlines() if line.strip()]
    markers = re.findall(r"<verification_(?:passed|failed|unavailable)>", output)
    if not nonempty or len(markers) != 1:
        return False, "Verifier output must contain exactly one terminal decision marker"
    if nonempty[-1] == "<verification_passed>":
        return True, ""
    if nonempty[-1] == "<verification_failed>":
        return False, "\n".join(nonempty[:-1]).strip() or "Verifier reported failure"
    if nonempty[-1] == "<verification_unavailable>":
        return False, "\n".join(nonempty[:-1]).strip() or "Independent review could not be completed"
    return False, "Verifier decision marker must be the exact final non-empty line"
