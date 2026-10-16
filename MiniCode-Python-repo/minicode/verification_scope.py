"""Choose native project checks from the task and Runtime-observed changes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import json
import os
import re

from minicode.path_safety import is_unsafe_stored_path
from minicode.workspace_provider import workspace_snapshot


@dataclass(frozen=True, slots=True)
class ProjectCheckScope:
    build: bool
    browser: bool
    screenshots: str
    reason: str
    changed_paths: tuple[str, ...] | None = None
    unit: bool = True

    def to_dict(self):
        return asdict(self)


def observed_changes(workspace, baseline):
    if baseline is None:
        return None
    current = workspace_snapshot(Path(workspace), reject_aliases=True)
    return tuple(sorted(name for name in set(baseline) | set(current)
                        if baseline.get(name) != current.get(name)))


_DOC = re.compile(r"readme|documentation|\bdocs?\b|文档|说明书|说明文件", re.I)
_WEB = re.compile(r"网页|页面|界面|前端|表单|弹窗|按钮|网站|web(?:site)?|\bUI\b|browser|build the site", re.I)
_VISUAL = re.compile(r"布局|样式|配色|字体|主题|视觉|外观|截图|layout|style|theme|appearance|screenshot", re.I)
_REVIEW = re.compile(r"截图|看图|视觉检查|检查界面|screenshot|visual (?:check|review)|review.*appearance", re.I)
_NO_SCREENSHOTS = re.compile(
    r"(?:不要|无需|不需要|不必|禁止|不用|不生成|不保存)\s*(?:自动|额外|任何)?\s*(?:截图|截屏|看图|视觉检查)|"
    r"\b(?:no|without)\s+(?:screenshots?|screen captures?)\b|"
    r"\b(?:do not|don't|skip|avoid)\s+(?:(?:take|taking|save|saving|capture|capturing|any)\s+)*screenshots?\b",
    re.I)
_VISUAL_EXT = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".styl",
               ".tsx", ".jsx", ".vue", ".svelte", ".svg", ".png", ".jpg", ".jpeg", ".webp"}
_DOC_EXT = {".md", ".rst", ".adoc"}
_WEB_PACKAGES = {"react", "vue", "svelte", "next", "nuxt", "@angular/core", "vite"}
_EXCLUDED = {"node_modules", "dist", ".git", ".temp", "Data", "__pycache__"}
_NON_UI = {"tests", "test", "server", "backend", "docs"}
_WEB_ACTION = re.compile(r"(?:创建|制作|开发|新增|构建|重做|优化|调整|修复).{0,24}(?:网页|网站|界面|前端|布局|样式|表单|按钮)|"
                         r"(?:build|create|redesign|fix|change|add).{0,32}(?:site|page|\bUI\b|layout|form|button)", re.I)


def _document(name):
    path = PurePosixPath(name)
    return path.suffix.lower() in _DOC_EXT or (
        path.suffix.lower() in {"", ".txt"}
        and path.stem.casefold() in {"readme", "changelog", "license", "licence", "copying", "notice"})


def _frontend(workspace):
    try:
        package = json.loads((workspace / "package.json").read_text(encoding="utf-8-sig"))
        dependencies = set(package.get("dependencies", {})) | set(package.get("devDependencies", {}))
        if dependencies & _WEB_PACKAGES:
            return True
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return any((workspace / name).is_file() for name in ("index.html", "public/index.html", "vite.config.ts",
                                                       "vite.config.js", "next.config.js", "next.config.ts"))


def _imported_by_ui(workspace, names):
    """Follow local source imports, so shared config/logic can affect the UI."""
    targets = {str((workspace / name).absolute()).casefold() for name in names}
    queue = []
    if (workspace / "index.html").is_file():
        queue.append(workspace / "index.html")
    # Bounded discovery within conventional frontend source trees. Unknown
    # structures retain a build check; this graph never certifies acceptance.
    for directory in ("src", "app", "pages", "components", "public"):
        root = workspace / directory
        if not root.is_dir() or is_unsafe_stored_path(root, boundary=workspace):
            continue
        visited = 0
        for directory, children, files in os.walk(root, topdown=True, followlinks=False):
            current = Path(directory)
            children[:] = [child for child in children if child not in _EXCLUDED
                           and not is_unsafe_stored_path(current / child, boundary=workspace)]
            visited += len(files)
            if len(queue) >= 256 or visited > 2048:
                break
            queue.extend(current / name for name in files if Path(name).suffix.lower() in _VISUAL_EXT)
    seen = set()
    while queue and len(seen) < 256:
        path = queue.pop()
        identity = str(path.absolute()).casefold()
        if identity in seen or is_unsafe_stored_path(path, boundary=workspace, reject_hardlinks=True):
            continue
        seen.add(identity)
        try:
            with path.open(encoding="utf-8") as stream:
                text = stream.read(65536)
        except (OSError, UnicodeError):
            continue
        references = re.findall(r"""(?:from\s*|import\s+|import\s*\(|require\s*\(|fetch\s*\()\s*['"]([^'"]+)['"]""", text)
        if path.suffix.lower() in {".html", ".htm"}:
            references.extend(re.findall(r"""(?:src|href)\s*=\s*['"]([^'"]+)['"]""", text))
        for item in references:
            if item.startswith("@/"):
                base = workspace / "src" / item[2:]
            elif item.startswith("/"):
                base = workspace / "public" / item.lstrip("/")
            elif item.startswith(".") or path.suffix.lower() in {".html", ".htm"} and not re.match(r"\w+:", item):
                base = path.parent / item
            else:
                continue
            if item.startswith("/") and (workspace / item.lstrip("/")).is_file():
                base = workspace / item.lstrip("/")
            candidates = [base, *(base.with_suffix(ext) for ext in (".ts", ".js", ".tsx", ".jsx", ".json")),
                          base / "index.ts", base / "index.js"]
            for candidate in candidates:
                if str(candidate.absolute()).casefold() in targets or str(candidate.resolve()).casefold() in targets:
                    return True
                if candidate.is_file() and candidate.suffix in {".ts", ".js", ".tsx", ".jsx", ".vue", ".svelte"}:
                    queue.append(candidate)
    return False


def select_project_scope(workspace, contract=None, *, changed_paths=None):
    root = Path(workspace).absolute()
    text = "" if contract is None else "\n".join(
        (contract.envelope.request, *contract.acceptance_criteria, *contract.expected_outputs))
    # An explicit prohibition must not become a positive screenshot request
    # merely because it contains the same keyword.
    no_screenshots = bool(_NO_SCREENSHOTS.search(text))
    text = _NO_SCREENSHOTS.sub("", text)
    names = None if changed_paths is None else tuple(sorted(set(name.replace("\\", "/") for name in changed_paths)))
    web_goal, visual_goal = bool(_WEB.search(text)), bool(_VISUAL.search(text))
    request = contract.envelope.request if contract is not None else ""
    explicit_browser = bool(_REVIEW.search(text))
    docs_goal = bool(_DOC.search(request)) and not explicit_browser and not _WEB_ACTION.search(request)
    frontend = _frontend(root)
    affected_ui = bool(names and _imported_by_ui(root, names))
    if names and all(_document(name) for name in names) and not affected_ui and not explicit_browser and (docs_goal or not web_goal):
        return ProjectCheckScope(False, False, "none", "Only documentation changed; no browser acceptance requested", names, unit=False)
    if not names and docs_goal:
        return ProjectCheckScope(False, False, "none", "Documentation task; no observed UI impact or browser acceptance", names, unit=False)
    visual_files = bool(names and any(PurePosixPath(name).suffix.lower() in _VISUAL_EXT
                                    and not {part.casefold() for part in PurePosixPath(name).parts} & _NON_UI
                                    for name in names))
    visual_config = bool((frontend or affected_ui or web_goal) and names and any(
        re.search(r"(?:locales?|i18n|themes?)[/._-]", name, re.I)
        and not {part.casefold() for part in PurePosixPath(name).parts} & _NON_UI for name in names))
    frontend_paths = bool(frontend and names and any(name.startswith(("src/", "app/", "pages/", "components/", "public/"))
                                                    and not name.startswith(("src/server/", "src/backend/"))
                                                    for name in names))
    build_config = bool(names and any(PurePosixPath(name).name in {
        "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "vite.config.js",
        "webpack.config.js", "next.config.js", "next.config.ts"} for name in names))
    browser = explicit_browser or web_goal and not docs_goal or visual_files or visual_config or affected_ui or frontend_paths or frontend and build_config
    visual = visual_files or visual_config or visual_goal and browser and not docs_goal
    if browser:
        return ProjectCheckScope(True, True, "none" if no_screenshots else "all" if visual else "failure",
            "Visual task or UI files changed" if visual else "Browser behavior/dependencies affected; capture failures only", names)
    if names and all(PurePosixPath(name).suffix.lower() in {".py", ".sql", ".md", ".rst"} for name in names):
        return ProjectCheckScope(False, False, "none", "Backend/document changes use their registered/unit/API checks", names)
    return ProjectCheckScope(True, False, "none", "Build check applies; no observed browser or visual requirement", names)
