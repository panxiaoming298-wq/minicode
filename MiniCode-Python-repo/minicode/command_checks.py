"""Read a declared, fresh check report separately from process exit success.

Reports are script-produced evidence, not independent Runtime acceptance.
Output keywords are deliberately not interpreted as executable check results.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json

from minicode.workspace import resolve_tool_path

REPORT_SCHEMA = "minicode.check-report.v1"
MAX_REPORT_BYTES = 262144


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field in check report")
        result[key] = value
    return result


class CommandCheckReport:
    def __init__(self, context, cwd, name):
        self.context = replace(context, cwd=cwd)
        self.name = name
        self.path = resolve_tool_path(self.context, name, "read")
        self.before = self._signature()

    def _signature(self):
        from minicode.path_safety import is_unsafe_stored_path
        if is_unsafe_stored_path(self.path, boundary=self.path.parent, reject_hardlinks=True):
            raise ValueError("check report must be a regular file without filesystem aliases")
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        if not self.path.is_file():
            raise ValueError("check report path must name a file")
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def finish(self, result):
        receipt = {"path": str(self.path), "status": "unverified"}
        try:
            if resolve_tool_path(self.context, self.name, "read") != self.path:
                raise ValueError("check report path changed during execution")
            signature = self._signature()
            if signature is None or signature == self.before:
                raise ValueError("command did not produce a fresh check report")
            if signature[1] > MAX_REPORT_BYTES:
                raise ValueError("check report exceeds 256 KiB")
            with self.path.open("rb") as stream:
                data = stream.read(MAX_REPORT_BYTES + 1)
            if len(data) > MAX_REPORT_BYTES or self._signature() != signature:
                raise ValueError("check report changed while being read or exceeds 256 KiB")
            report = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_object)
            if (not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA
                    or report.get("completed") is not True):
                raise ValueError("check report must declare its schema and completed=true")
            checks = report.get("checks")
            if not isinstance(checks, list) or not 1 <= len(checks) <= 256:
                raise ValueError("check report must contain 1 to 256 explicit checks")
            names = set()
            for check in checks:
                if (not isinstance(check, dict) or not isinstance(check.get("name"), str)
                        or not 1 <= len(check["name"].strip()) <= 200
                        or type(check.get("passed")) is not bool):
                    raise ValueError("each check needs a nonblank name and a boolean passed result")
                if check["name"].strip() in names:
                    raise ValueError("check names must be unique")
                names.add(check["name"].strip())
            failed = [check["name"].strip() for check in checks if not check["passed"]]
            receipt.update(status="failed" if failed else "passed", total=len(checks),
                           failed=len(failed), failedChecks=failed[:8], sha256=hashlib.sha256(data).hexdigest())
        except (OSError, ValueError, TypeError, RecursionError) as error:
            receipt["reason"] = str(error)[:500]
        passed = receipt["status"] == "passed"
        guidance = (
            "\nThe command's exit code and output are preserved separately. check_report only reads a report "
            "explicitly produced by a custom script; it does not make pytest, ruff or npm test generate one. "
            "If this option was added to an ordinary test-runner command, use its retained process output "
            "and obtain any required native verification through the registered checks. Do not rerun an "
            "unchanged command solely to look for a report it never produces. If the report is required, "
            "repair its producer and rerun the check; never fabricate a report or waive a required check."
            if receipt["status"] == "unverified" else ""
        )
        return replace(result, ok=result.ok and passed,
            verificationFailed=result.verificationFailed or receipt["status"] == "failed",
            failureKind=("invalid_check_report" if result.ok and receipt["status"] == "unverified" else result.failureKind),
            commandCheckReport=receipt,
            output=result.output + "\n\nCommand check report (script-produced evidence): "
                   + json.dumps(receipt, ensure_ascii=False) + guidance)
