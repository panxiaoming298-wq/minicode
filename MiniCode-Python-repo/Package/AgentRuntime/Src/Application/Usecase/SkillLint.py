"""Inspect a package without activating instructions or executing scripts."""
import re
from urllib.parse import unquote, urlsplit
from ..Port.Out.SkillStorage import SkillSources


class LintSkill:
    def __init__(self, sources: SkillSources):
        self.sources = sources

    def execute(self, name):
        findings = []

        def finding(code, severity, message):
            findings.append({"code": code, "severity": severity, "message": message})

        try:
            document = self.sources.load(name)
        except (OSError, ValueError, RuntimeError) as error:
            finding("source", "error", str(error))
            return {"name": name, "ok": False, "findings": findings}
        description = document.description
        if description == "No description provided." or len(description) < 16:
            finding("description", "warning", "Describe when to use the Skill and when to exclude it")
        if len(description) > 240:
            finding("description_length", "warning", "Long descriptions may be omitted as whole cards from the catalog")
        if len(document.body.encode("utf-8")) > 24576:
            finding("body_length", "warning", "Long required body; move conditional material into declared branches")
        for message in document.metadata.diagnostics:
            finding("metadata", "error" if message.startswith("Unsupported execution field:") else "warning", message)
        if re.search(r"!`[^`]*`", document.body):
            finding("shell_macro", "error", "Shell pre-execution macros are unsupported and will not run")
        required = set(document.metadata.required_resources)
        for paths in document.metadata.branches.values():
            required.update(paths)
        # Deliberately one level: navigation links do not recursively include
        # instructions. Only explicitly declared resources are mandatory.
        links = set()
        for value in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.body):
            target = value.strip().split(' "', 1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme in {"http", "https", "mailto"} or not parsed.path:
                continue
            links.add(unquote(parsed.path))
        for path in sorted(links | required):
            try:
                if path in required:
                    self.sources.resource(document, path)
                else:
                    self.sources.validate_resource(document, path)
            except (OSError, ValueError, RuntimeError) as error:
                finding("resource", "error", path + ": " + str(error))
        return {"name": name, "source_hash": document.source_hash,
                "ok": not any(f["severity"] == "error" for f in findings), "findings": findings}
