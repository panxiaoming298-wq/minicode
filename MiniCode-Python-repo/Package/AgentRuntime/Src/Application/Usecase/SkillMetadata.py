"""Validate portable fields separately from the versioned MiniCode contract."""
import re
from ..Dto.Skill import SkillMetadata


class ValidateSkillMetadata:
    def execute(self, metadata, *, fallback_description="No description provided."):
        if not isinstance(metadata, dict) or any(not isinstance(k, str) for k in metadata):
            raise ValueError("Skill front matter must be a mapping with string keys")
        for key in ("name", "description", "license", "compatibility"):
            if key in metadata and (not isinstance(metadata[key], str) or not metadata[key].strip()):
                raise ValueError(f"Skill {key} must be a nonempty string")
        if metadata.get("name") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", metadata["name"]):
            raise ValueError("Invalid Skill metadata name")
        for key in ("disable-model-invocation", "user-invocable"):
            if key in metadata and type(metadata[key]) is not bool:
                raise ValueError(f"Skill {key} must be a boolean")
        extras = metadata.get("metadata", {})
        if not isinstance(extras, dict):
            raise ValueError("Skill metadata must be a mapping")
        native = extras.get("minicode", {})
        if not isinstance(native, dict) or (native and (type(native.get("schema_version")) is not int or native["schema_version"] != 1)):
            raise ValueError("metadata.minicode requires schema_version: 1")
        supported = {"schema_version", "required_resources", "branches", "required_tools", "conflicts"}
        if set(native) - supported:
            raise ValueError("Unsupported metadata.minicode fields: " + ", ".join(sorted(set(native) - supported)))

        def strings(value, label):
            if not isinstance(value, list) or len(value) > 32 or any(not isinstance(x, str) or not x.strip() for x in value):
                raise ValueError(f"Skill {label} must be a list of at most 32 nonempty strings")
            if len(set(value)) != len(value):
                raise ValueError(f"Skill {label} contains duplicates")
            return tuple(value)

        required = strings(native.get("required_resources", []), "required_resources")
        branches = native.get("branches", {})
        if not isinstance(branches, dict) or len(branches) > 32:
            raise ValueError("Skill branches must be a mapping of at most 32 branches")
        normalized = {}
        for name, paths in branches.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("Invalid Skill branch name")
            normalized[name] = strings(paths, "branch resources")
        diagnostics = []
        if "allowed-tools" in metadata:
            allowed = metadata["allowed-tools"]
            if not isinstance(allowed, str) and not (isinstance(allowed, list) and all(isinstance(v, str) for v in allowed)):
                raise ValueError("allowed-tools must be a string or list of strings")
            diagnostics.append("allowed-tools is informational; existing Runtime permissions still apply")
        for key in ("context", "agent", "model", "hooks"):
            if key in metadata:
                diagnostics.append(f"Unsupported execution field: {key}")
        return SkillMetadata(description=metadata.get("description", fallback_description).strip(),
            name=metadata.get("name", ""), disable_model_invocation=metadata.get("disable-model-invocation", False),
            user_invocable=metadata.get("user-invocable", True), fields=metadata,
            required_resources=required, branches=normalized,
            required_tools=strings(native.get("required_tools", []), "required_tools"),
            conflicts=strings(native.get("conflicts", []), "conflicts"), diagnostics=tuple(diagnostics))
