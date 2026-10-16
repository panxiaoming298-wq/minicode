"""Request-bound schema admission and bounded, non-executing correction guidance."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
import time


def _digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError, RecursionError, OverflowError):
        # Malformed inputs are still rejected by the owning parser/validator.
        # Diagnostics must never replace that receipt with a crash or false link.
        return None


def request_contracts(registry):
    # Copy hashes before the model call; discover_tools in the returned batch
    # cannot retroactively change which schemas the model actually received.
    return {tool.name: digest for tool in registry.list() if (digest := _digest(tool.input_schema)) is not None}


def schema_required(tool, supplied):
    return supplied is not None and (tool.name not in supplied or supplied[tool.name] != _digest(tool.input_schema))


def _type(value):
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer",
            float: "number", type(None): "null"}.get(type(value), type(value).__name__)


def input_issues(value, schema):
    """Explain a validator rejection, not a second validator or MCP interpreter.

    Only constraints understood by the native validator are explained. Refs and
    unsupported extensions stay with their owner. Never rewrite arguments.
    """
    issues = []

    def add(path, expected, actual, example=None):
        item = {"path": list(path), "expected": expected, "actual": actual}
        if example is not None:
            item["example"] = example
        issues.append(item)

    def walk(item, rule, path=()):
        if len(issues) >= 4 or len(path) > 8 or not isinstance(rule, dict):
            return
        if any(key in rule for key in ("$ref", "not")):
            return
        # Native conditional contracts use the same evaluator for admission
        # and diagnostics. Unsupported extension schemas remain owner-validated.
        from minicode.tools._validation import validate_contract
        for branch in rule.get("allOf", []):
            walk(item, branch, path)
        if "if" in rule:
            try:
                validate_contract(item, rule["if"])
            except ValueError:
                walk(item, rule.get("else", {}), path)
            except RuntimeError:
                pass
            else:
                walk(item, rule.get("then", {}), path)
        for union in ("anyOf", "oneOf"):
            if union in rule:
                matches = []
                try:
                    for branch in rule[union]:
                        try:
                            validate_contract(item, branch)
                        except ValueError:
                            continue
                        matches.append(branch)
                except RuntimeError:
                    continue
                if not matches or union == "oneOf" and len(matches) != 1:
                    add(path, "one allowed shape" if union == "oneOf" else "an allowed shape", actual=_type(item))
        declared = rule.get("type")
        types = declared if isinstance(declared, list) else [declared] if declared else []
        actual = _type(item)
        if types and actual not in types and not (actual == "integer" and "number" in types):
            kind = types[0]
            example = {"string": "<string>", "integer": math.ceil(rule.get("minimum", 0)),
                       "number": rule.get("minimum", 0), "boolean": False, "array": [], "object": {}}.get(kind)
            add(path, " or ".join(types), actual, example)
            return
        if "enum" in rule and not any(type(item) is type(option) and item == option for option in rule["enum"]):
            add(path, "one of " + json.dumps(rule["enum"], ensure_ascii=True)[:300], actual)
            return
        if "const" in rule and (type(item) is not type(rule["const"]) or item != rule["const"]):
            add(path, "constant " + json.dumps(rule["const"], ensure_ascii=True)[:300], actual)
            return
        if type(item) is dict:
            props = rule.get("properties", {})
            for key in rule.get("required", [])[:32]:
                if key not in item and len(issues) < 4:
                    add((*path, key), "required " + str(props.get(key, {}).get("type", "field")), "missing")
            for key, child in list(item.items())[:64]:
                if len(issues) >= 4:
                    break
                if key not in props and rule.get("additionalProperties") is False:
                    add((*path, key), "no extra field; allowed: " + ", ".join(props)[:350], "unexpected field")
                elif key in props:
                    walk(child, props[key], (*path, key))
        elif type(item) is list and isinstance(rule.get("items"), dict):
            for index, child in enumerate(item[:64]):
                walk(child, rule["items"], (*path, index))
        for lower, upper, amount in (("minimum", "maximum", item if type(item) in (int, float) else None),
                                      ("minLength", "maxLength", len(item) if type(item) is str else None),
                                      ("minItems", "maxItems", len(item) if type(item) is list else None)):
            if amount is not None and len(issues) < 4:
                if lower in rule and amount < rule[lower]:
                    add(path, f"{lower}={rule[lower]}", str(amount))
                elif upper in rule and amount > rule[upper]:
                    add(path, f"{upper}={rule[upper]}", str(amount))

    walk(value, schema)
    return issues[:4]


def correction_guidance(tool, arguments, result):
    if not result.inputValidationFailed or result.ok or result.inputSchemaRequired:
        return result
    try:
        issues = list(result.inputIssues) or input_issues(arguments, tool.input_schema)
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
        issues = []  # External schemas may use unsupported constraints.
    guidance = {"tool": tool.name, "issues": issues,
        "next_action": "Correct the reported fields using the current tool schema and original intended operation. "
                       "Examples show types only; never invent paths, source IDs or revisions. Nothing is auto-corrected."}
    if not issues:
        guidance["next_action"] = "Follow the original validator error above. Refresh referenced state if needed; " \
                                  "the JSON shape alone cannot prove that paths, IDs or revisions are valid."
    if any(issue["actual"] == "unexpected field" for issue in issues):
        guidance["next_action"] += " Remove unsupported keys entirely; assigning null does not remove a key."
    if tool.name == "memory_note_update" and any(issue["actual"] == "unexpected field" for issue in issues):
        guidance["structure_example"] = {"confirmed_findings": [
            {"text": "<one concise finding>", "source_event_ids": ["<existing current-task source_event_id>"]},
            {"text": "<another finding>", "source_event_ids": ["<existing current-task source_event_id>"]}]}
        guidance["next_action"] += " Put separate findings in separate array items, never text_2/text_3 fields."
    hint = "Tool input correction (guidance only): " + json.dumps(guidance, ensure_ascii=True)
    return replace(result, inputIssues=issues, inputCorrectionHint=hint, output=result.output + "\n\n" + hint)


def issue_fingerprints(issues):
    """Group the same broken rule across array positions and unrelated text edits.

    Diagnostic identity only: this never establishes an operation's identity,
    marks a prior call recovered, or permits a replay.
    """
    return tuple(sorted({json.dumps([
        ["*" if type(part) is int else part for part in issue.get("path", [])],
        issue.get("expected", "")], sort_keys=True, ensure_ascii=True)
        for issue in issues if isinstance(issue, dict) and issue.get("expected")
        and isinstance(issue.get("path"), list)}))


def _masked_fingerprint(arguments, paths, tool_name):
    if not isinstance(arguments, dict) or any(not path for path in paths):
        return None
    # Unchanged options such as "verbose" cannot establish the same operation.
    # Require known native identity fields; unfamiliar tools use exact input only.
    anchors = {"read_file": ("path",), "write_file": ("path",),
               "edit_file": ("path", "old"), "list_files": ("path",),
               "runtime_context_read": ("handle", "start"),
               "memory_note_update": ("expected_revision",)}.get(tool_name)
    if not anchors or any(key not in arguments or any(path[0] == key for path in paths) for key in anchors):
        return None
    masked = deepcopy(arguments)
    for path in paths:
        parent = masked
        try:
            for key in path[:-1]:
                parent = parent[key]
            if isinstance(parent, dict):
                parent.pop(path[-1], None)
            elif isinstance(parent, list):
                parent[path[-1]] = None
        except (KeyError, IndexError, TypeError):
            pass
    # At least one unchanged argument must identify the intended operation.
    if not masked:
        return None
    return _digest(masked)


class ToolInputRecovery:
    """Diagnostics scoped to one activation; no capability or replay authority."""
    def __init__(self):
        self.pending = []
        self._schema_loads = []
        self.counts = dict(calls=0, successes=0, schemaDeferrals=0, invalidCalls=0, correctionAttempts=0,
                           recoveredCalls=0, correctionSuccesses=0, firstPassSuccesses=0, repeatedInvalidCalls=0,
                           recoveryRequests=0, recoveryModelMs=0, recoveryTokens=0)
        self.tokens_known = True

    @property
    def recovering(self):
        return bool(self._schema_loads or any(item["status"] == "invalid" for item in self.pending))

    def schemas_supplied(self, contracts, step):
        """A later answered request carried the exact schema; no tool ran here."""
        loaded = []
        for item in self._schema_loads[:]:
            if item["step"] < step and item["schema"] and contracts.get(item["tool"]) == item["schema"]:
                loaded.append((item["callId"], item["tool"], {**item["feedback"], "schemaLoadedAtStep": step}))
                self._schema_loads.remove(item)
        return loaded

    def model_observed(self, usage, elapsed_ms, was_recovering):
        if not was_recovering:
            return
        self.counts["recoveryRequests"] += 1
        self.counts["recoveryModelMs"] += max(0, round(elapsed_ms))
        if isinstance(usage, dict) and usage.get("tokens_known") and type(usage.get("tokens")) is int:
            self.counts["recoveryTokens"] += usage["tokens"]
        else:
            self.tokens_known = False

    def observe(self, call, result, step, *, tool=None):
        now = time.monotonic()
        rejected_arguments = bool(call.get("inputError") or call.get("executionBlocked"))
        args_hash = _digest(["rejected", call.get("rawArguments"), call.get("executionBlocked"), call.get("inputError")]
                            if rejected_arguments else ["parsed", call["input"]])
        name = call["toolName"]
        revision = call["input"].get("expected_revision") if name == "memory_note_update" and isinstance(call["input"], dict) else None
        note_saved = False
        if type(revision) is int and revision >= 0 and result.ok:
            try:
                receipt = json.loads(result.output)
                note_saved = (receipt.get("status") == "saved" and receipt.get("revision") == revision + 1
                              and bool(receipt.get("source_event_id")))
            except (ValueError, TypeError, AttributeError):
                pass
        matches = [item for item in self.pending if args_hash is not None and item["tool"] == name and item["step"] < step
                   and (item["arguments"] == args_hash or item["masked"] is not None
                        and _masked_fingerprint(call["input"], item["paths"], name) == item["masked"]
                        or note_saved and item["noteRevision"] == revision)]
        # Two distinct rejected proposals could match the same corrected shape.
        # Do not guess which operation a success resolves.
        if len({item["arguments"] for item in matches}) > 1 and not note_saved:
            matches = []
        self.counts["calls"] += 1
        if matches:
            self.counts["correctionAttempts"] += 1
        settled_success = result.ok and not (result.awaitUser or result.runtimeYield or result.backgroundTask
                                             or result.timedOut or result.ambiguousSideEffect)
        status = "success" if settled_success else "schema_required" if result.inputSchemaRequired \
            else "invalid" if result.inputValidationFailed else "other"
        metadata = {"status": status, "step": step, "correctsCallIds": [], "corrects": [], "issues": result.inputIssues}
        if result.inputSchemaRequired:
            self._schema_loads.append(dict(tool=name, callId=call["id"], step=step,
                schema=_digest(tool.input_schema) if tool is not None else None, feedback=dict(metadata)))
            self._schema_loads = self._schema_loads[-64:]
        if rejected_arguments and result.inputValidationFailed:
            from minicode.tool_argument_diagnostics import rejected_argument_details
            metadata["argumentError"] = rejected_argument_details(call)
        if settled_success:
            self.counts["successes"] += 1
            if matches:
                metadata["correctsCallIds"] = [item["callId"] for item in matches]
                metadata["corrects"] = [{"callId": item["callId"], "step": item["step"]} for item in matches]
                metadata["recoveryElapsedMs"] = round((now - min(item["time"] for item in matches)) * 1000)
                self.counts["recoveredCalls"] += len(matches)
                self.counts["correctionSuccesses"] += 1
                self.pending = [item for item in self.pending if item not in matches]
            else:
                self.counts["firstPassSuccesses"] += 1
        elif status in {"invalid", "schema_required"}:
            self.counts["schemaDeferrals" if status == "schema_required" else "invalidCalls"] += 1
            if status == "invalid" and args_hash is not None and any(item["arguments"] == args_hash for item in matches):
                self.counts["repeatedInvalidCalls"] += 1
            paths = [issue["path"] for issue in result.inputIssues]
            self.pending.append(dict(tool=name, callId=call["id"], arguments=args_hash, step=step, time=now,
                                     status=status,
                                     noteRevision=revision if type(revision) is int and revision >= 0 else None,
                                     paths=paths, masked=_masked_fingerprint(call["input"], paths, name)
                                     if paths and not rejected_arguments else None))
            self.pending = self.pending[-64:]
        metadata["metrics"] = self.metrics()
        return metadata

    def metrics(self):
        initial_calls = self.counts["calls"] - self.counts["correctionAttempts"]
        return {**self.counts,
            "firstPassRate": self.counts["firstPassSuccesses"] / initial_calls if initial_calls else None,
            "correctionSuccessRate": self.counts["correctionSuccesses"] / self.counts["correctionAttempts"] if self.counts["correctionAttempts"] else None,
            "recoveryTokens": self.counts["recoveryTokens"] if self.tokens_known else None,
            "matchingScope": "conservatively matched calls in this activation; unmatched retries count as new calls",
            "usageScope": "whole observed model requests while corrections were pending; not incremental correction cost"}
