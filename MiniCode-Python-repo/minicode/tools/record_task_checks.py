"""Record functional and visual findings without conflating their evidence."""
import json

from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.task_checks import REPORT_TOOL, TaskCheckEvidence
from minicode.tooling import ToolCapability, ToolDefinition, ToolMetadata, ToolResult
from minicode.tools._validation import validate_contract

CHECK = {"type": "object", "additionalProperties": False,
    "required": ["status", "summary", "evidence_call_ids"], "properties": {
        "status": {"type": "string", "enum": ["passed", "failed", "not_checked", "not_applicable"],
            "description": "not_checked: relevant but unperformed or missing evidence. not_applicable: outside this turn's deliverables and acceptance criteria, never a substitute for an unavailable check."},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000,
            "description": "Concrete tested criteria, findings and limitations, or why this check was not performed. Do not just say PASS."},
        "evidence_call_ids": {"type": "array", "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": "Functional: settled command/test/registered-check calls. Visual: view_image calls whose pixels you already received. Empty for not_checked and not_applicable."}}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "anyOf": [{"required": ["functional"]}, {"required": ["visual"]}],
          "properties": {"functional": CHECK, "visual": CHECK}}


def validate(value):
    validate_contract(value, SCHEMA)
    for check in value.values():
        if not check["summary"].strip():
            raise ValueError("Check summary must contain concrete findings or the reason it was not checked")
        if len(set(check["evidence_call_ids"])) != len(check["evidence_call_ids"]):
            raise ValueError("Do not repeat evidence call IDs")
        if check["status"] in {"not_checked", "not_applicable"} and check["evidence_call_ids"]:
            raise ValueError("Unchecked or inapplicable checks must have empty evidence_call_ids")
    return value


def run(value, context):
    ledger = (context._runtime or {}).get("_taskCheckEvidence")
    if not isinstance(ledger, TaskCheckEvidence):
        ledger = TaskCheckEvidence()
    try:
        report = ledger.record(value, context)
        output = json.dumps(report, ensure_ascii=False)
        if len(output) > 28_000:
            raise ValueError("Check report is too large; summarize findings and cite fewer, focused evidence calls")
        return ToolResult(ok=True, output=output)
    except ValueError as error:
        return ToolResult(ok=False, output=f"Cannot save this check report: {error}. Continue the remaining task and explain the recording limitation in the final response; do not loop on recording.")


record_task_checks_tool = ToolDefinition(name=REPORT_TOOL,
    description="Save a snapshot of task-relevant check findings before final delivery. Include at least one category and all applicable categories; omit unrelated categories. "
        "Nonvisual documentation, OpenSpec, backend or CLI work does not require a visual entry or a recording call merely to explain its absence. "
        "Use not_applicable only when explicitly documenting why a category is outside this turn's scope. Cite native tool call IDs from this activation. "
        "Visual verdicts require a previous view_image call whose actual pixels you received, plus concrete observations. "
        "Use not_checked with the reason for a required check when evidence is missing or the model cannot see images. Missing or outdated evidence downgrades only that category to not_checked; other findings are kept and work can continue. This records a scoped AI assessment, not automatic whole-task acceptance.",
    input_schema=SCHEMA, validator=validate, run=run,
    metadata=ToolMetadata(name=REPORT_TOOL, description="Record task-relevant check findings",
        capabilities={ToolCapability.READ_ONLY}, effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.NEVER_AUTO_RETRY))
