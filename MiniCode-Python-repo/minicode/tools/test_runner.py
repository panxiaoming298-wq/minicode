from __future__ import annotations

import os
import re
import subprocess
import sys
import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import Any

from minicode.tooling import ToolDefinition, ToolResult
from minicode.tools._validation import validate_contract
from minicode.tools.run_command import _run_with_bounded_pipes
from minicode.workspace import resolve_tool_path


TEST_RUNNER_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1, "default": ".",
                 "description": "Directory or file path to test (default: current directory)"},
        "framework": {"type": "string", "enum": ["auto", "pytest", "unittest"], "default": "auto",
                      "description": "Test framework to use (default: auto)"},
        "verbose": {"type": "boolean", "default": False, "description": "Show detailed output"},
        "coverage": {"type": "boolean", "default": False,
                     "description": "Enable coverage reporting (requires pytest-cov)"},
        "pattern": {"type": ["string", "null"],
                    "description": "Filter tests by name pattern; omit or use null for all tests"},
        "timeout": {"type": "integer", "minimum": 10, "maximum": 300, "default": 60,
                    "description": "Timeout in whole seconds (10-300, default: 60)"},
    },
}


# ---------------------------------------------------------------------------
# Test Discovery
# ---------------------------------------------------------------------------

def _discover_test_files(path: Path, pattern: str = "test_*.py", *, cancellation_event=None,
                         limit: int | None = None) -> list[Path]:
    """Discover test files matching pattern."""
    test_files: list[Path] = []
    
    if path.is_file():
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            test_files.append(path)
    elif path.is_dir():
        for root, dirs, files in os.walk(path):
            if cancellation_event is not None and cancellation_event.is_set():
                raise InterruptedError("test discovery canceled")
            # Skip common non-test dirs
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv", "env", ".tox", "node_modules")]
            
            for f in files:
                if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                    test_files.append(Path(root) / f)
                    if limit is not None and len(test_files) >= limit:
                        return test_files
    
    return sorted(test_files)


# ---------------------------------------------------------------------------
# Test Output Parsers
# ---------------------------------------------------------------------------

def _parse_pytest_output(output: str) -> dict[str, Any]:
    """Parse pytest output into structured format."""
    results: dict[str, Any] = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "warnings": 0,
        "tests": [],
        "failures": [],
        "coverage": None,
    }
    
    # Parse summary line
    summary_match = re.search(r'(\d+) passed', output)
    if summary_match:
        results["passed"] = int(summary_match.group(1))
    
    failed_match = re.search(r'(\d+) failed', output)
    if failed_match:
        results["failed"] = int(failed_match.group(1))
    
    error_match = re.search(r'(\d+) error', output)
    if error_match:
        results["errors"] = int(error_match.group(1))
    
    skipped_match = re.search(r'(\d+) skipped', output)
    if skipped_match:
        results["skipped"] = int(skipped_match.group(1))
    
    warning_match = re.search(r'(\d+) warning', output)
    if warning_match:
        results["warnings"] = int(warning_match.group(1))
    
    # Parse individual test results
    test_pattern = re.compile(r'(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(.+?)(?:::(\w+))?', re.MULTILINE)
    for match in test_pattern.finditer(output):
        status = match.group(1)
        file_path = match.group(2)
        test_name = match.group(3)
        
        results["tests"].append({
            "file": file_path.strip(),
            "name": test_name or "unknown",
            "status": status.lower(),
        })
    
    # Parse failure details
    failure_pattern = re.compile(r'FAILURES\s*\n(.*?)(?=\n={50,}|\Z)', re.DOTALL)
    failure_match = failure_pattern.search(output)
    if failure_match:
        results["failure_details"] = failure_match.group(1)[:2000]
    
    # Parse coverage if present
    coverage_pattern = re.compile(r'TOTAL\s+\d+\s+\d+\s+(\d+)%')
    coverage_match = coverage_pattern.search(output)
    if coverage_match:
        results["coverage"] = int(coverage_match.group(1))
    
    return results


def _parse_unittest_output(output: str) -> dict[str, Any]:
    """Parse unittest output into structured format."""
    results: dict[str, Any] = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "tests": [],
        "failures": [],
    }
    
    # Parse summary
    summary_match = re.search(r'Ran (\d+) test', output)
    if summary_match:
        total = int(summary_match.group(1))
        if "OK" in output:
            results["passed"] = total
        else:
            failed_match = re.search(r'failures=(\d+)', output)
            error_match = re.search(r'errors=(\d+)', output)
            
            results["failed"] = int(failed_match.group(1)) if failed_match else 0
            results["errors"] = int(error_match.group(1)) if error_match else 0
            results["passed"] = total - results["failed"] - results["errors"]
    
    return results


# ---------------------------------------------------------------------------
# Tool Implementation
# ---------------------------------------------------------------------------

def _validate(input_data: dict) -> dict:
    validate_contract(input_data, TEST_RUNNER_INPUT)
    path = input_data.get("path", ".")
    framework = input_data.get("framework", "auto")
    verbose = input_data.get("verbose", False)
    coverage = input_data.get("coverage", False)
    pattern = input_data.get("pattern")
    timeout = input_data.get("timeout", 60)
    
    return {
        "path": path,
        "framework": framework,
        "verbose": verbose,
        "coverage": coverage,
        "pattern": pattern,
        "timeout": timeout,
    }


def _run(input_data: dict, context) -> ToolResult:
    """Run tests with smart discovery and parsing."""
    try:
        target = resolve_tool_path(context, input_data["path"], "test")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    framework = input_data["framework"]
    verbose = input_data["verbose"]
    coverage = input_data["coverage"]
    pattern = input_data.get("pattern")
    timeout = input_data["timeout"]
    
    if not target.exists():
        return ToolResult(ok=False, output=f"Path not found: {target}")
    
    # Discover test files
    cancellation = getattr(context, "cancellation_event", None)
    try:
        # Only establish that the target has tests. The framework performs the
        # complete discovery once; counting every file here doubles broad scans.
        test_files = _discover_test_files(target, cancellation_event=cancellation, limit=1)
    except InterruptedError as error:
        return ToolResult(ok=False, output=str(error), failureKind="canceled", commandExecutionSettled=True)
    if cancellation is not None and cancellation.is_set():
        return ToolResult(ok=False, output="Test execution canceled before process start", failureKind="canceled",
                          commandExecutionSettled=True)
    
    if not test_files:
        return ToolResult(
            ok=False,
            output=f"No test files found in {input_data['path']}\n\n"
                   f"Expected files matching: test_*.py or *_test.py",
        )
    
    # Determine framework
    if framework == "auto":
        framework = "pytest" if importlib.util.find_spec("pytest") is not None else "unittest"
    
    # Build command
    if framework == "pytest":
        cmd = [sys.executable, "-m", "pytest"]
        
        # Pass the target once so pytest performs its complete discovery without
        # hitting platform command-line limits for large suites.
        cmd.append(str(target))
        
        if verbose:
            cmd.append("-v")
        
        if coverage:
            cmd.extend(["--cov", str(target)])
        
        # Add pattern
        if pattern:
            cmd.extend(["-k", pattern])
        
    else:  # unittest
        if target.is_file():
            cmd = [sys.executable, "-m", "unittest", str(target)]
        else:
            cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(target)]
        
        if pattern and target.is_dir():
            cmd.extend(["-p", f"*{pattern}*"])
        elif target.is_dir():
            cmd.extend(["-p", "test_*.py"])
        
        if verbose:
            cmd.append("-v")
    
    # Run tests
    lines = [
        "🧪 Test Runner",
        "=" * 60,
        "",
        f"Framework: {framework}",
        f"Target: {input_data['path']}",
        f"Pattern: {pattern or 'all'}",
        f"Coverage: {'enabled' if coverage else 'disabled'}",
        "",
        "-" * 60,
        "",
    ]
    
    result = None
    try:
        result = _run_with_bounded_pipes(
            cmd[0], cmd[1:],
            cwd=str(context.cwd),
            timeout=timeout,
            cancellation_event=cancellation,
        )
        if result.timedOut or result.failureKind == "canceled" or result.ambiguousSideEffect:
            return replace(result, output="\n".join(lines) + "\n" + result.output)
        output = result.output
        success = result.ok
        
        # Parse results
        if framework == "pytest":
            parsed = _parse_pytest_output(output)
        else:
            parsed = _parse_unittest_output(output)
        
        # Format results
        lines.append("📊 Results:")
        lines.append(f"  ✓ Passed:  {parsed.get('passed', 0)}")
        lines.append(f"  ✗ Failed:  {parsed.get('failed', 0)}")
        lines.append(f"  ⚠ Errors:  {parsed.get('errors', 0)}")
        lines.append(f"  ⊘ Skipped: {parsed.get('skipped', 0)}")
        
        if parsed.get("coverage"):
            lines.append(f"  📈 Coverage: {parsed['coverage']}%")
        
        lines.append("")
        
        # Show failures
        if parsed.get("failed", 0) > 0 or parsed.get("errors", 0) > 0:
            lines.append("❌ Failures:")
            
            if parsed.get("failure_details"):
                lines.append(parsed["failure_details"][:2000])
            else:
                # Extract from output
                failure_pattern = re.compile(r'FAILURES\s*\n(.*?)(?=\n={50,}|\Z)', re.DOTALL)
                failure_match = failure_pattern.search(output)
                if failure_match:
                    lines.append(failure_match.group(1)[:2000])
            
            lines.append("")
        
        # Show warnings
        if parsed.get("warnings", 0) > 0:
            lines.append(f"⚠️  {parsed['warnings']} warning(s)")
            lines.append("")
        
        # Show test list if verbose
        if verbose and parsed.get("tests"):
            lines.append("📝 All Tests:")
            for test in parsed["tests"][:50]:  # Limit to 50
                icon = {"passed": "✓", "failed": "✗", "error": "⚠", "skipped": "⊘"}.get(test["status"], "?")
                lines.append(f"  {icon} {test['file']}::{test['name']}")
            if len(parsed["tests"]) > 50:
                lines.append(f"  ... and {len(parsed['tests']) - 50} more")
            lines.append("")
        
        # Full output if requested
        if verbose:
            lines.append("-" * 60)
            lines.append("")
            lines.append("Full Output:")
            lines.append(output[:5000])
            if len(output) > 5000:
                lines.append("\n... (output truncated)")
        
    except subprocess.TimeoutExpired:
        lines.append(f"❌ Tests timed out after {timeout} seconds")
        success = False
    except Exception as e:
        lines.append(f"❌ Test execution error: {e}")
        success = False
    
    if result is not None:
        return replace(result, ok=success, output="\n".join(lines))
    return ToolResult(ok=success, output="\n".join(lines))


test_runner_tool = ToolDefinition(
    name="test_runner",
    description="Discover and run Python tests automatically. Supports pytest and unittest frameworks. Provides structured results with pass/fail counts, failure details, and optional coverage reporting.",
    input_schema=TEST_RUNNER_INPUT,
    validator=_validate,
    run=_run,
)
