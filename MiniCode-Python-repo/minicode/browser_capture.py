"""Reusable Playwright checkpoints and image-bound capture provenance.

Scenario authors keep ordinary Playwright code. A checkpoint records only its
explicit assertions; neither screenshots nor these reports certify AI vision.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from minicode.path_safety import is_unsafe_stored_path

CAPTURE_SCHEMA = "minicode.browser-capture.v1"
MAX_CAPTURE_BYTES = 6000


def public_url(url: str) -> str:
    """Keep the page identity without persisting credentials/query tokens."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def capture_metadata(value, data: bytes) -> dict | None:
    """Metadata is script-produced context, never an execution attestation."""
    if not isinstance(value, dict) or value.get("schema") != CAPTURE_SCHEMA:
        return None
    if value.get("imageSha256") != hashlib.sha256(data).hexdigest():
        return None
    strings = ("url", "scenario", "view", "browser", "browserVersion",
               "sourceRevision", "sourceBinding", "region", "readyCondition")
    if any(not isinstance(value.get(key), str) or len(value[key]) > 1000 for key in strings):
        return None
    viewport = value.get("viewport")
    if (not isinstance(viewport, dict) or any(type(viewport.get(key)) is not int
            or not 1 <= viewport[key] <= 8192 for key in ("width", "height"))
            or type(value.get("capturedAt")) is not int or value["capturedAt"] <= 0
            or type(value.get("ready")) is not bool
            or type(value.get("deviceScaleFactor")) not in (int, float)
            or not 0 < value["deviceScaleFactor"] <= 10):
        return None
    clean = {key: value[key] for key in strings}
    clean.update(schema=CAPTURE_SCHEMA, imageSha256=value["imageSha256"],
                 viewport={key: viewport[key] for key in ("width", "height")},
                 capturedAt=value["capturedAt"], ready=value["ready"],
                 deviceScaleFactor=value["deviceScaleFactor"])
    try:
        clean["url"] = public_url(clean["url"])
    except ValueError:
        return None
    return clean if len(json.dumps(clean).encode()) <= MAX_CAPTURE_BYTES else None


def read_capture_metadata(path: Path, data: bytes) -> dict | None:
    sidecar = path.with_name(path.name + ".capture.json")
    try:
        if is_unsafe_stored_path(sidecar, boundary=Path(sidecar.anchor), reject_hardlinks=True):
            return None
        with sidecar.open("rb") as stream:
            raw = stream.read(MAX_CAPTURE_BYTES + 1)
        if len(raw) > MAX_CAPTURE_BYTES:
            return None
        return capture_metadata(json.loads(raw), data)
    except (OSError, ValueError, TypeError, RecursionError):
        return None  # Old/malformed sidecars never hide the actual image.


def _atomic_write(path: Path, data: bytes) -> None:
    if is_unsafe_stored_path(path, boundary=Path(path.anchor), reject_hardlinks=True):
        raise ValueError("Capture output contains filesystem aliases")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class BrowserCapture:
    def __init__(self, output: Path, *, view: str, browser: str, browser_version: str,
                 source_revision: str = "", source_binding: str = "unknown", timeout: int = 10000,
                 animations: str = "disabled", screenshots: str = "all", criteria=()):
        self.output = Path(output).absolute()
        if is_unsafe_stored_path(self.output, boundary=Path(self.output.anchor)):
            raise ValueError("Capture output contains filesystem aliases")
        self.output.mkdir(parents=True, exist_ok=True)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", view):
            raise ValueError("Use a short filename-safe view name")
        if (not 1 <= timeout <= 30000 or animations not in {"allow", "disabled"}
                or screenshots not in {"all", "failure", "none"}):
            raise ValueError("Invalid checkpoint timeout or animation policy")
        self.view, self.browser, self.browser_version = view, browser, browser_version
        self.source_revision, self.source_binding = source_revision, source_binding
        self.timeout, self.animations = timeout, animations
        self.screenshots, self.criteria = screenshots, tuple(criteria)
        self.checks: list[dict] = []
        self.images: list[dict] = []
        self._names: set[str] = set()

    def check(self, name: str, passed: bool, detail: str = "", *, covers=()) -> None:
        qualified = f"{self.view}/{name}"
        if (not name.strip() or len(qualified) > 200 or type(passed) is not bool
                or any(check["name"] == qualified for check in self.checks)):
            raise ValueError("Checks need unique short names and a boolean result")
        numbers = tuple(covers)
        if (len(set(numbers)) != len(numbers) or any(type(number) is not int or
                not 1 <= number <= len(self.criteria) for number in numbers)):
            raise ValueError("covers must use distinct numbers from the declared task criteria")
        self.checks.append({"name": qualified, "passed": passed, "detail": str(detail)[:2000],
                            **({"covers": list(numbers)} if numbers else {})})

    def criterion(self, number: int, assertion, *, name: str | None = None) -> bool:
        """Execute an assertion for a declared task criterion; retain failures."""
        if type(number) is not int or not 1 <= number <= len(self.criteria) or not callable(assertion):
            raise ValueError("criterion requires a declared criterion number and callable assertion")
        detail = ""
        try:
            outcome = assertion()
            if outcome is not None and type(outcome) is not bool:
                raise ValueError("An assertion must return None or a boolean")
            passed = outcome is not False
        except Exception as error:
            passed, detail = False, str(error)
        self.check(name or f"criterion-{number}", passed, detail, covers=(number,))
        return passed

    def checkpoint(self, page, name: str = "page", *, ready=None, target=None,
                   visible: dict | None = None, check_overflow: bool = True, force: bool = False) -> dict | None:
        """Capture current state; the caller performs clicks/scrolls with Playwright.

        ready/target and visible values are ordinary Playwright Locators.
        A region target scrolls into view BEFORE readiness and layout checks.
        Visibility checks never scroll their own locators.
        """
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name.casefold() in self._names:
            raise ValueError("Checkpoint names must be unique and filename-safe")
        if len(self._names) >= 12:
            raise ValueError("At most 12 checkpoints per view")
        # Windows and common macOS filesystems treat case-only paths as equal.
        self._names.add(name.casefold())
        ready_ok, detail = True, ""
        # Locator repr includes its frame URL (possibly query credentials).
        condition = ("explicit locator visibility; " if ready is not None else "") + "rendered content, visible images and fonts"
        try:
            if ready is not None:
                ready.wait_for(state="visible", timeout=self.timeout)
            if target is not None:
                # Locator.screenshot() scrolls too. Do it before observations
                # so lazy-loaded images and visibility refer to the crop state.
                target.scroll_into_view_if_needed(timeout=self.timeout)
            page.wait_for_function("""() => {
                const body = document.body;
                return body && (body.innerText.trim().length > 0 ||
                    [...body.querySelectorAll('canvas,svg,img')].some(el => {
                        const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
                    }));
            }""", polling=100, timeout=self.timeout)
            page.wait_for_function("""() =>
                (!document.fonts || document.fonts.status === 'loaded') &&
                [...document.images].every(img => {
                    const r = img.getBoundingClientRect(), s = getComputedStyle(img);
                    return !r.width || !r.height || r.bottom <= 0 || r.top >= innerHeight ||
                        r.right <= 0 || r.left >= innerWidth || s.visibility === 'hidden' ||
                        s.display === 'none' || (img.complete && img.naturalWidth > 0);
                })""", polling=100, timeout=self.timeout)
        except Exception as error:
            ready_ok, detail = False, str(error)
        self.check(f"{name}/ready", ready_ok, detail)
        if check_overflow:
            try:
                dimensions = page.evaluate("""() => ({
                    content: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
                    viewport: document.documentElement.clientWidth
                })""")
                self.check(f"{name}/horizontal-overflow",
                           dimensions["content"] <= dimensions["viewport"] + 1, json.dumps(dimensions))
            except Exception as error:
                self.check(f"{name}/horizontal-overflow", False, str(error))
        for label, locator in (visible or {}).items():
            try:
                observation = locator.evaluate("""el => {
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    const inViewport = r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
                        r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1;
                    const points = [[.5,.5],[.15,.15],[.85,.15],[.15,.85],[.85,.85]];
                    let opaque = true;
                    for (let ancestor = el; ancestor; ancestor = ancestor.parentElement) {
                        if (Number(getComputedStyle(ancestor).opacity) === 0) { opaque = false; break; }
                    }
                    const reachable = inViewport && points.every(([x,y]) => {
                        const top = document.elementFromPoint(r.left+r.width*x, r.top+r.height*y);
                        return top && (top === el || el.contains(top));
                    });
                    return {visible: inViewport && reachable && opaque && s.visibility === 'visible' &&
                        s.display !== 'none' && Number(s.opacity) > 0,
                        rect: {x:r.x,y:r.y,width:r.width,height:r.height}, reachable};
                }""", timeout=self.timeout)
                self.check(f"{name}/visible/{label}", observation["visible"], json.dumps(observation))
            except Exception as error:
                self.check(f"{name}/visible/{label}", False, str(error))
        filename = f"{self.view}.png" if name == "page" else f"{self.view}-{name}.png"
        path = self.output / filename
        if self.screenshots == "none" or (self.screenshots == "failure" and not force
                and all(check["passed"] for check in self.checks)):
            return None  # A check-only checkpoint claims no screenshot execution.
        try:
            subject = target if target is not None and ready_ok else page
            data = subject.screenshot(type="png", timeout=self.timeout, animations=self.animations,
                                      caret="hide", scale="css")
            geometry = page.evaluate("""() => ({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})""")
            capture = {
                "schema": CAPTURE_SCHEMA, "imageSha256": hashlib.sha256(data).hexdigest(),
                "url": public_url(page.url), "scenario": name, "view": self.view,
                # Mobile overflow can enlarge innerWidth via automatic page
                # scaling. Keep the configured device viewport as provenance.
                "viewport": page.viewport_size or {"width": geometry["width"], "height": geometry["height"]},
                "deviceScaleFactor": geometry["dpr"], "browser": self.browser,
                "browserVersion": self.browser_version, "sourceRevision": self.source_revision,
                "sourceBinding": self.source_binding, "capturedAt": round(time.time() * 1000),
                "region": "element" if subject is not page else "viewport",
                "ready": ready_ok, "readyCondition": condition,
            }
            if capture_metadata(capture, data) is None:
                raise ValueError("Invalid capture provenance")
            # Publish provenance before the PNG so a command image observer
            # never sees a freshly completed PNG with unfinished metadata.
            _atomic_write(path.with_name(filename + ".capture.json"), json.dumps(capture).encode())
            _atomic_write(path, data)
            image = {"name": filename, "path": str(path), "capture": capture}
            self.images.append(image)
            self.check(f"{name}/screenshot", True)
            return image
        except Exception as error:
            self.check(f"{name}/screenshot", False, str(error))
            return None


def criterion_results(criteria, required, checks, views):
    """Describe actual mapped assertions; no implicit business coverage."""
    from minicode.runtime_protocol import acceptance_criterion_ids
    result = []
    for number, (text, identifier) in enumerate(zip(criteria, acceptance_criterion_ids(tuple(criteria))), 1):
        mapped = [check for check in checks if number in check.get("covers", ())]
        missing = [view for view in views if not any(check["name"].startswith(view + "/") for check in mapped)]
        result.append({"number": number, "id": identifier, "text": text, "required": number in required,
            "status": "failed" if any(not check["passed"] for check in mapped) else
                      "not_checked" if missing else "passed",
            "checks": [check["name"] for check in mapped], "missing_views": missing})
    return result
