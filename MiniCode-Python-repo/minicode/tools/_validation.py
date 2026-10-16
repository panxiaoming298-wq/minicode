"""Shared, non-coercing checks for native tool contracts and flat utility inputs."""

from __future__ import annotations

import math
from typing import Any


class InputContractError(ValueError):
    """A native constraint rejected before the requested operation has effects."""

    def __init__(self, message: str, *, path: list, expected: str, actual: str):
        super().__init__(message)
        self.issues = [{"path": path, "expected": expected, "actual": actual}]


def normalize_aliases(input_data: Any, aliases: dict[str, str], *, path: str = "$") -> dict:
    """Preserve documented legacy names without silently choosing conflicting values."""
    if not isinstance(input_data, dict):
        raise ValueError(f"{path}: expected an object")
    result = dict(input_data)
    for alias, canonical in aliases.items():
        if alias not in result:
            continue
        value = result.pop(alias)
        if canonical in result and (type(result[canonical]) is not type(value) or result[canonical] != value):
            raise ValueError(f"{path}.{canonical}: conflicts with legacy field {alias}; provide one value")
        result[canonical] = value
    return result


def validate_contract(value: Any, schema: dict, *, path: str = "$") -> None:
    """Validate the small, explicit schema vocabulary used by native tools.

    Uses the same definition sent to the model. No coercion, default insertion,
    field removal, or extension/MCP schema interpretation happens here.
    Unsupported schema keywords are a programming error, never silently ignored.
    Semantic checks and compatibility aliases stay in the owning native tool.
    """
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum",
                 "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
                 "anyOf", "oneOf", "allOf", "if", "then", "else", "const",
                 "description", "default", "examples", "title"}
    if set(schema) - supported:
        raise RuntimeError(f"Unsupported native contract keywords: {sorted(set(schema) - supported)}")
    for branch in schema.get("allOf", []):
        validate_contract(value, branch, path=path)
    if "if" in schema:
        try:
            validate_contract(value, schema["if"], path=path)
        except ValueError:
            branch = schema.get("else", {})
        else:
            branch = schema.get("then", {})
        validate_contract(value, branch, path=path)
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError(f"{path}: expected constant {schema['const']!r}")
    for union in ("anyOf", "oneOf"):
        if union in schema:
            successes, errors = 0, []
            for branch in schema[union]:
                try:
                    validate_contract(value, branch, path=path)
                    successes += 1
                except ValueError as error:
                    errors.append(str(error))
            if not successes or (union == "oneOf" and successes != 1):
                raise ValueError(f"{path}: expected {union} the allowed shapes; " + "; ".join(errors))
    types = {"object": (dict,), "array": (list,), "string": (str,), "integer": (int,),
             "number": (int, float), "boolean": (bool,), "null": (type(None),)}
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if any(name not in types for name in names):
            raise RuntimeError(f"Unsupported native contract types: {names}")
        if not any(type(value) in types[name] for name in names):
            raise ValueError(f"{path}: expected {' or '.join(names)}, got {type(value).__name__}; do not coerce the value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}: must be finite")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        detail = f". {schema['description']}" if schema.get("description") else ""
        raise ValueError(f"{path}: expected one of {schema['enum']}{detail}")
    if type(value) is dict:
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        extra = sorted(str(key) for key in value if key not in properties)
        if missing:
            raise ValueError(f"{path}: missing required fields: {', '.join(missing)}")
        if extra and schema.get("additionalProperties") is False:
            raise ValueError(f"{path}: unexpected fields: {', '.join(extra)}. Allowed fields: {', '.join(properties)}")
        for key, item in value.items():
            if key in properties:
                validate_contract(item, properties[key], path=f"{path}.{key}")
    if type(value) is list:
        for bound, comparison in (("minItems", lambda n, b: n < b), ("maxItems", lambda n, b: n > b)):
            if bound in schema and comparison(len(value), schema[bound]):
                raise ValueError(f"{path}: {len(value)} entries violates {bound}={schema[bound]}")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_contract(item, schema["items"], path=f"{path}[{index}]")
    if type(value) is str:
        for bound, comparison in (("minLength", lambda n, b: n < b), ("maxLength", lambda n, b: n > b)):
            if bound in schema and comparison(len(value), schema[bound]):
                raise ValueError(f"{path}: {len(value)} characters violates {bound}={schema[bound]}")
    if type(value) in (int, float):
        for bound, comparison in (("minimum", lambda n, b: n < b), ("maximum", lambda n, b: n > b)):
            if bound in schema and comparison(value, schema[bound]):
                raise ValueError(f"{path}: {value} violates {bound}={schema[bound]}")


def validate_fields(input_data: Any, *, required: tuple[str, ...] = (), **fields: type | tuple[type, ...]) -> None:
    """Check presence and exact JSON types before a utility normalizes values.

    Semantic checks (encodings, delimiters, ranges, paths) stay with each tool.
    This is deliberately not a JSON Schema interpreter for extension tools.
    """
    if not isinstance(input_data, dict):
        raise ValueError("input must be an object")
    missing = [name for name in required if name not in input_data]
    if missing:
        raise ValueError("Missing required fields: " + ", ".join(missing))
    unknown = set(input_data) - fields.keys()
    if unknown:
        raise ValueError("Unknown fields: " + ", ".join(sorted(map(str, unknown))))
    for name, item in input_data.items():
        expected = fields[name]
        types = expected if isinstance(expected, tuple) else (expected,)
        if type(item) not in types:
            raise ValueError(f"{name} must be " + " or ".join(t.__name__ for t in types))
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{name} must be finite")
