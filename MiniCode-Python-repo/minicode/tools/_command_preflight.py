"""Conservative checks for explicit commands; never rewrite or execute a script."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

from minicode.tooling import ToolResult
from minicode.tools._validation import InputContractError


def program_name(command):
    return command.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def validate_shell_arguments(command, args):
    if program_name(command) not in {"powershell", "pwsh"}:
        return
    for index, arg in enumerate(args):
        if arg.lower() in {"-command", "-c", "-file", "-f", "-encodedcommand", "-enc"}:
            break
        if arg.lower() in {"-lc", "-ic", "-cil"}:
            raise InputContractError(
                "PowerShell does not accept POSIX shell flags; use -NoProfile -Command <script>. No command was executed.",
                path=["args", index], expected="PowerShell switches (-NoProfile -Command or -File)", actual=arg)


def is_file_search(command, args):
    name = program_name(command)
    if name == "find":
        # Windows find.exe searches text; only explicit POSIX find options
        # identify filesystem discovery on that host.
        return os.name != "nt" or any(arg in {"-name", "-iname", "-path", "-maxdepth", "-type", "-print", "-exec"} for arg in args)
    if name in {"fd", "fdfind"}:
        return True
    if name in {"rg", "ripgrep"}:
        return "--files" in args
    return name in {"powershell", "pwsh"} and bool(re.search(
        r"(?i)^\s*(?:Get-ChildItem|gci|dir)\b", _powershell_script(args) or ""))


def _powershell_script(args):
    for index, arg in enumerate(args[:-1]):
        if arg.lower() in {"-command", "-c"}:
            return " ".join(args[index + 1:])
    return None


def _repository_exists(cwd):
    for path in (cwd, *cwd.parents):
        if (path / ".git").is_dir() or (path / ".git").is_file():
            return True
        if (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir():
            return True  # Bare repositories have no .git child.
    return False


def _ignore_check_directory(args, cwd):
    """Resolve literal Git -C options; leave explicit repository overrides to Git."""
    if os.environ.get("GIT_DIR") or os.environ.get("GIT_WORK_TREE"):
        return None
    root, index = cwd, 0
    while index < len(args):
        option = args[index]
        if option == "-C" and index + 1 < len(args):
            root = (root / args[index + 1]).resolve()
            if not root.is_dir():
                return None  # Preserve Git's error for an invalid directory.
            index += 2
        elif option in {"--no-pager", "--no-optional-locks"}:
            index += 1
        else:
            if option != "check-ignore" or index + 1 == len(args):
                return None
            options = args[index + 1:]
            if "--" in options:
                options = options[:options.index("--")]
            if any(arg in {"--help", "-h"} for arg in options):
                return None
            return root if root.is_dir() else None
    return None


def check_command(command, args, *, cwd, use_shell=False) -> ToolResult | None:
    """Inspect known literal forms after permission admission, before spawning.

    Complex shell programs are left to their shell and bounded process runner.
    This is an ergonomics guard, not a shell sandbox or a permission decision.
    An inapplicable ignore check returns a skip notice without process evidence.
    """
    if use_shell:
        return
    name, root = program_name(command), Path(cwd).resolve()
    validate_shell_arguments(command, args)
    if shutil.which(command) is None:
        raise InputContractError(
            f"Executable not found: {command}. Check the installed executable/path before retrying; no process started.",
            path=["command"], expected="an installed executable or its explicit path", actual=command)
    # Only the entry point is a file. Wildcards passed to the script itself,
    # inline code, module mode, test runners and unknown flags are untouched.
    if name in {"node", "nodejs", "python", "python3", "py"}:
        harmless = {"--no-warnings", "--trace-warnings", "--enable-source-maps", "-u", "-B", "-I", "-S"}
        for index, arg in enumerate(args):
            if arg in harmless:
                continue
            if arg.startswith("-"):
                break
            if any(mark in arg for mark in ("*", "?", "[")) and not (root / arg).is_file():
                raise InputContractError(
                    "The interpreter entry point is an unexpanded file pattern, not a script. Use list_files for filename discovery, "
                    "read_file/grep_files for source, or the project's declared build/check command. No process started.",
                    path=["args", index], expected="one concrete script entry point", actual=arg)
            break
    ignore_root = _ignore_check_directory(args, root) if name == "git" else None
    if ignore_root is not None and not _repository_exists(ignore_root):
        return ToolResult(ok=True, output=(
            "当前目录未启用 Git，已跳过忽略规则验证。\n"
            f"目录：{ignore_root}\n"
            "未执行 git check-ignore，此结果不表示忽略规则验证通过。可直接检查 .gitignore 的内容。"))
    if (name == "git" and args and args[0] in {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files"}
            and not any(arg in {"--help", "-h"} for arg in args)
            and not (args[0] == "diff" and "--no-index" in args)
            and not (args[0] == "rev-parse" and any(arg in {"--parseopt", "--sq-quote"} for arg in args))
            and not os.environ.get("GIT_DIR") and not os.environ.get("GIT_WORK_TREE") and not _repository_exists(root)):
        raise InputContractError(
            "The selected directory is not inside a Git repository. Inspect files directly or select the actual repository; "
            "do not initialize a repository just to suppress this diagnostic. No Git command was executed.",
            path=["cwd"], expected="a Git repository for repository-specific commands", actual=str(root))
    validate_filename_discovery(name, args, root)


def validate_filename_discovery(name, args, root):
    """Check recognized literal filename scans without requiring a shell."""
    root = Path(root).resolve()
    if name in {"powershell", "pwsh"}:
        script = _powershell_script(args) or ""
        # Recognize a literal, leading enumeration only. Quoted text, variables
        # and arbitrary nested scripts must not be misclassified as commands.
        if (re.match(r"(?i)^\s*(?:Get-ChildItem|gci|dir)\b", script)
                and re.search(r"(?i)(?:^|\s)-Recurse\b", script)
                and not re.search(r"(?i)(?:^|\s)-Depth\s+\d+\b", script)):
            paths = re.search(r"(?i)\s-Path\s+((?:'[^']*'|\"[^\"$`]*\")(?:\s*,\s*(?:'[^']*'|\"[^\"$`]*\"))*)", script)
            if paths:
                literals = re.findall(r"'([^']*)'|\"([^\"$`]*)\"", paths[1])
                for pair in literals:
                    literal = Path(pair[0] or pair[1])
                    target = (literal if literal.is_absolute() else root / literal).resolve()
                    if not target.is_relative_to(root):
                        raise InputContractError(
                            "Recursive filename discovery outside the current workspace needs a bounded scope. "
                            "Use list_files with one path, pattern, recursive=true and limits, or narrow the command and specify -Depth. "
                            "Use attached image references directly. No process started.",
                            path=["args"], expected="bounded recursion for external filename discovery", actual="external -Recurse without -Depth")
