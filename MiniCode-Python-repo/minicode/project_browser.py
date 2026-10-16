"""Runtime-owned browser smoke check, executed in a cancellable child process."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import sys
import threading
from urllib.parse import urlsplit

# The native verifier invokes this owned script with -I from another project.
# Resolve our package from this file, never from the candidate's cwd.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minicode.browser_capture import BrowserCapture, criterion_results, public_url


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _linked(path: Path) -> bool:
    # Python 3.11 on Windows has no Path.is_junction(). Reparse attributes
    # cover junctions as well as symlinks, including the dist root itself.
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def inspect_site(root: Path, output: Path, **options) -> dict:
    if root.exists() and _linked(root):
        raise RuntimeError("Browser check refuses linked build outputs")
    if not (root / "index.html").is_file():
        raise RuntimeError("Build produced no dist/index.html; this check supports static npm frontends only")
    # Serving generated symlinks could expose files outside the verification copy.
    for path in root.rglob("*"):
        if _linked(path):
            raise RuntimeError("Browser check refuses linked build outputs")
    output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        return inspect_url(origin, output, **options)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _origin(address: str, schemes=("http", "https")) -> str:
    parsed = urlsplit(address)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an explicit HTTP(S) URL without embedded credentials")
    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


def _runtime_check(capture, name, passed, detail=""):
    """Keep runner observations even when a scenario chose the same name."""
    existing = {check["name"] for check in capture.checks}
    candidate, number = name, 1
    while f"{capture.view}/{candidate}" in existing:
        number += 1
        candidate = f"{name}/runtime-{number}"
    capture.check(candidate, passed, detail)
    return f"{capture.view}/{candidate}"


def inspect_url(url: str, output: Path, *, scenario: Path | None = None, source_root: Path | None = None,
                source_revision: str = "", source_binding: str = "unknown", ready_selector: str | None = None,
                browser_channel: str = "auto", allowed_origins=(), allowed_websockets=(),
                timeout: int = 10000, views=("desktop", "mobile"), animations: str = "disabled",
                color_scheme: str = "light", screenshots: str = "all", trace: str = "failure",
                criteria=(), required_criteria=None) -> dict:
    """Inspect a running service. Routing limits page traffic, not Python scripts."""
    from playwright.sync_api import sync_playwright
    from minicode.workspace_provider import snapshot_revision, workspace_snapshot

    origins = {_origin(url), *(_origin(item) for item in allowed_origins)}
    sockets = {_origin(item, ("ws", "wss")) for item in allowed_websockets}
    if not views or any(view not in {"desktop", "mobile"} for view in views) or len(set(views)) != len(views):
        raise ValueError("Choose desktop and/or mobile once each")
    if browser_channel not in {"auto", "chromium", "msedge", "chrome"}:
        raise ValueError("Unsupported browser channel")
    if not 1 <= timeout <= 30000 or color_scheme not in {"light", "dark"}:
        raise ValueError("Invalid timeout or color scheme")
    if screenshots not in {"all", "failure", "none"} or trace not in {"failure", "none"}:
        raise ValueError("Invalid screenshot or trace policy")
    if (not isinstance(criteria, (list, tuple)) or len(criteria) > 64
            or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in criteria)
            or len(set(criteria)) != len(criteria)):
        raise ValueError("Declare at most 64 distinct nonblank task criteria")
    required = tuple(range(1, len(criteria) + 1)) if required_criteria is None else tuple(required_criteria)
    if (len(set(required)) != len(required) or any(type(number) is not int or
            not 1 <= number <= len(criteria) for number in required)):
        raise ValueError("requiredCriteria must name distinct declared criterion numbers")
    if source_root is not None:
        source_root = source_root.resolve()
        if not source_root.is_dir():
            raise ValueError("source_root must be an existing source directory")
        relative = output.resolve().relative_to(source_root) if output.resolve().is_relative_to(source_root) else None
        if relative is not None and (not relative.parts or relative.parts[0] != ".temp"):
            raise ValueError("Keep capture outputs outside source_root (or in its ignored .temp directory)")
        source_revision = snapshot_revision(workspace_snapshot(source_root, reject_aliases=True))
        # Observing workspace files does not prove what a remote/stale server runs.
        source_binding = "workspace-observed"
    inspect = None
    if scenario is not None:
        inspect = runpy.run_path(str(scenario.resolve())).get("inspect")
        if not callable(inspect):
            raise ValueError("Scenario must define inspect(page, capture) using ordinary Playwright")
    result = {"schema": "minicode.check-report.v1", "completed": False,
              "scope": "browser-scenarios" if inspect else "browser-smoke",
              "interaction_coverage": False, "visualCheck": "not_checked",
              "source_revision": source_revision, "source_binding": source_binding,
              "views": [], "errors": [], "checks": [], "images": [], "traces": [],
              "screenshot_policy": screenshots, "coverage_scope": "declared-scenario-assertions"}
    with sync_playwright() as playwright:
        browser, launch_errors = None, []
        channels = (None, "msedge", "chrome") if browser_channel == "auto" else (
            None if browser_channel == "chromium" else browser_channel,)
        for channel in channels:
            try:
                browser = playwright.chromium.launch(headless=True, channel=channel, timeout=15000)
                result["browser"] = channel or "chromium"
                break
            except Exception as error:
                launch_errors.append(str(error)[:4000])
        if browser is None:
            raise RuntimeError("No usable browser. Install Chromium with python -m playwright install chromium "
                               "or install Edge/Chrome. " + "; ".join(launch_errors))
        try:
            for name in views:
                width, height = (1440, 900) if name == "desktop" else (390, 844)
                capture = BrowserCapture(output, view=name, browser=result["browser"],
                    browser_version=browser.version, source_revision=source_revision,
                    source_binding=source_binding, timeout=timeout, animations=animations,
                    screenshots=screenshots, criteria=criteria)
                errors, page, title = [], None, ""
                def add_error(error):
                    if len(errors) < 40:
                        errors.append(str(error)[:2000])
                device = dict(playwright.devices["Pixel 5"]) if name == "mobile" else {}
                context = browser.new_context(**{**device, "viewport": {"width": width, "height": height},
                    "service_workers": "block", "accept_downloads": False, "color_scheme": color_scheme,
                    "locale": "en-US", "timezone_id": "UTC", "reduced_motion": "reduce" if animations == "disabled" else "no-preference"})
                try:
                    if trace == "failure":
                        # DOM/network history is useful without recording
                        # unsolicited screenshot frames on successful tasks.
                        context.tracing.start(screenshots=False, snapshots=True, sources=False)
                    def route(request_route):
                        address = request_route.request.url
                        try:
                            permitted = _origin(address) in origins
                        except ValueError:
                            permitted = False
                        if permitted:
                            request_route.continue_()
                        else:
                            add_error("External request blocked: " + public_url(address))
                            request_route.abort()
                    def websocket(socket):
                        if _origin(socket.url, ("ws", "wss")) in sockets:
                            socket.connect_to_server()
                        else:
                            add_error("WebSocket blocked: " + public_url(socket.url))
                            # Not connecting prevents all traffic to the server.
                            # Sync close() inside Playwright's WebSocket callback
                            # can deadlock its dispatcher; context.close() tears
                            # down the intercepted socket after bounded checks.
                    context.route("**/*", route)
                    context.route_web_socket("**/*", websocket)
                    page = context.new_page()
                    page.set_default_timeout(timeout)
                    page.on("pageerror", lambda error: add_error("JavaScript: " + str(error)))
                    page.on("console", lambda message: add_error("Console: " + message.text)
                            if message.type == "error" else None)
                    page.on("response", lambda response: add_error(f"HTTP {response.status}: {public_url(response.url)}")
                            if response.status >= 400 else None)
                    page.on("requestfailed", lambda request: add_error(f"Request failed: {public_url(request.url)}"))
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                    title = page.title()
                    if inspect is not None:
                        if ready_selector:
                            page.locator(ready_selector).wait_for(state="visible", timeout=timeout)
                        inspect(page, capture)
                        if not capture.checks:
                            raise ValueError("Scenario produced no explicit checks")
                    else:
                        capture.checkpoint(page, ready=page.locator(ready_selector) if ready_selector else None)
                except Exception as error:
                    add_error(error)
                    if page is not None:
                        # Bound failure capture separately; keep earlier checkpoints.
                        capture.timeout = min(timeout, 2000)
                        try:
                            capture.checkpoint(page, "failure" if inspect else "page", check_overflow=False, force=True)
                        except ValueError:
                            pass  # Preserve prior checkpoints if all slots/names were used.
                finally:
                    browser_error_check = _runtime_check(capture, "browser-errors", not errors, "\n".join(errors))
                    for number in required:
                        mapped = [check for check in capture.checks if number in check.get("covers", ())]
                        if not mapped:
                            _runtime_check(capture, f"criterion-{number}/executed", False, "Required criterion has no mapped assertion")
                    failed = any(not check["passed"] for check in capture.checks)
                    if failed and page is not None and screenshots != "none" and not capture.images:
                        capture.timeout = min(timeout, 2000)
                        try:
                            capture.checkpoint(page, "failure", check_overflow=False, force=True)
                        except ValueError:
                            pass
                    if trace == "failure":
                        try:
                            if failed:
                                path = output / f"{name}-trace.zip"
                                context.tracing.stop(path=str(path))
                                if path.stat().st_size > 64 * 1024 * 1024:
                                    raise ValueError("Failure trace exceeds 64 MiB")
                                result["traces"].append({"name": path.name, "path": str(path), "view": name})
                            else:
                                context.tracing.stop()
                        except Exception as error:
                            _runtime_check(capture, "trace-save", False, str(error))
                    # Existing project-check cards render this error list.
                    # Keep explicit readiness/layout failures visible there too.
                    view_errors = (errors + [
                        f"{check['name']}: {check['detail'] or 'check failed'}"
                        for check in capture.checks
                        if not check["passed"] and check["name"] != browser_error_check
                    ])[:40]
                    result["checks"].extend(capture.checks)
                    result["images"].extend(capture.images)
                    result["views"].append({"name": name, "width": width, "height": height,
                        "title": title,
                        "screenshot": capture.images[0]["name"] if capture.images else None,
                        "checkpoints": [image["capture"]["scenario"] for image in capture.images],
                        "errors": view_errors})
                    result["errors"].extend(view_errors)
                    context.close()
        finally:
            browser.close()
    if source_root is not None:
        unchanged = snapshot_revision(workspace_snapshot(source_root, reject_aliases=True)) == source_revision
        result["checks"].append({"name": "source-unchanged", "passed": unchanged})
        result["source_unchanged"] = unchanged
        if not unchanged:
            result["errors"].append("source-unchanged: source files changed during browser checks")
    result["completed"] = True
    result["criterion_results"] = criterion_results(criteria, required, result["checks"], views)
    result["all_criteria_covered"] = bool(criteria) and all(
        item["status"] == "passed" for item in result["criterion_results"])
    result["passed"] = bool(result["checks"]) and all(check["passed"] for check in result["checks"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--root", type=Path, help="Static build containing index.html")
    target.add_argument("--url", help="Running application URL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, help="Python file defining inspect(page, capture)")
    parser.add_argument("--source-root", type=Path, help="Observe local source before/after (not a server version attestation)")
    parser.add_argument("--source-revision", default="", help="Source fingerprint supplied by the owning verifier")
    parser.add_argument("--source-binding", default="unknown", choices=("unknown", "built-candidate"))
    parser.add_argument("--ready-selector")
    parser.add_argument("--browser", default="auto", choices=("auto", "chromium", "msedge", "chrome"))
    parser.add_argument("--view", action="append", choices=("desktop", "mobile"))
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument("--allow-websocket", action="append", default=[])
    parser.add_argument("--timeout-ms", type=int, default=10000)
    parser.add_argument("--animations", choices=("disabled", "allow"), default="disabled")
    parser.add_argument("--color-scheme", choices=("light", "dark"), default="light")
    parser.add_argument("--screenshots", choices=("all", "failure", "none"), default="all")
    parser.add_argument("--trace", choices=("failure", "none"), default="failure")
    parser.add_argument("--criteria-file", type=Path, help="Frozen acceptanceCriteria and optional requiredCriteria JSON")
    args = parser.parse_args()
    try:
        declaration = {}
        if args.criteria_file:
            from minicode.path_safety import is_unsafe_stored_path
            path = args.criteria_file.absolute()
            if is_unsafe_stored_path(path, boundary=Path(path.anchor), reject_hardlinks=True):
                raise ValueError("Criteria file contains filesystem aliases")
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("Criteria file exceeds 64 KiB")
            declaration = json.loads(raw)
            if not isinstance(declaration, dict) or "acceptanceCriteria" not in declaration:
                raise ValueError("Criteria file must declare acceptanceCriteria")
        options = dict(scenario=args.scenario, source_root=args.source_root, source_revision=args.source_revision,
            source_binding=args.source_binding, ready_selector=args.ready_selector, browser_channel=args.browser,
            views=args.view or ("desktop", "mobile"), allowed_origins=args.allow_origin,
            allowed_websockets=args.allow_websocket, timeout=args.timeout_ms,
            animations=args.animations, color_scheme=args.color_scheme,
            screenshots=args.screenshots, trace=args.trace,
            criteria=declaration.get("acceptanceCriteria", ()), required_criteria=declaration.get("requiredCriteria"))
        result = (inspect_site(args.root.absolute(), args.output.absolute(), **options) if args.root
                  else inspect_url(args.url, args.output.absolute(), **options))
    except ImportError:
        result = {"passed": False, "errors": ["Browser dependency missing. Install minicode-py[web] (Playwright)."]}
    except Exception as error:
        result = {"passed": False, "errors": [f"{type(error).__name__}: {error}"]}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
