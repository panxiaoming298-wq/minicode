"""Observe declared command image outputs without delaying process cancellation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import threading

from minicode.browser_capture import read_capture_metadata
from minicode.run_images import MAX_TOOL_IMAGES, publish_image, read_image_file
from minicode.workspace import resolve_tool_path


class CommandImages:
    def __init__(self, context, cwd: str, paths: list[str]):
        self.context = context
        scoped = replace(context, cwd=cwd)
        self.paths = list(dict.fromkeys(resolve_tool_path(scoped, name, "read") for name in paths))
        self.before = {}
        self.seen = set()
        self.records = []
        self.errors = {}
        self.stop_event = threading.Event()
        self.thread = None
        for path in self.paths:
            try:
                self.before[path] = self.signature(path)
            except OSError:
                self.before[path] = None

    @staticmethod
    def signature(path):
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ino

    def scan(self):
        for path in self.paths:
            try:
                signature = self.signature(path)
                if signature == self.before[path]:
                    continue  # Do not label a stale pre-existing screenshot as this run's output.
                data = read_image_file(path)
                key = (str(path), hashlib.sha256(data).hexdigest())
                if key in self.seen:
                    continue
                if len(self.records) >= MAX_TOOL_IMAGES:
                    self.errors[path] = "本次命令已展示 24 张图片，后续图片保留在项目文件中。"
                    continue
                capture = read_capture_metadata(path, data)
                record = publish_image(self.context, data, name=path.name, tool_name="run_command",
                    caption=f"{capture['view']} · {capture['scenario']}" if capture else "", capture=capture)
                self.records.append(record)
                self.seen.add(key)
                self.errors.pop(path, None)
            except Exception as error:
                self.errors[path] = str(error)

    def __enter__(self):
        if self.paths:
            def watch():
                while not self.stop_event.wait(.3):
                    self.scan()
            self.thread = threading.Thread(target=watch, name="minicode-command-images", daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        if not self.thread or not self.thread.is_alive():
            self.scan()  # Capture short commands and completed outputs left by a failed process.

    def summary(self):
        missing = [path.name for path in self.paths if not any(str(path) == key[0] for key in self.seen)]
        lines = []
        if self.records:
            lines.append(f"[Images saved for the user: {len(self.records)}. Displaying an image does not constitute visual verification.]")
            lines.append("Saved image references for view_image(image_id=...): " + json.dumps([
                {"image_id": record["id"], "name": record["name"],
                 **({"capture": record["capture"]} if record.get("capture") else {})}
                for record in self.records], ensure_ascii=False))
        if missing:
            lines.append("[Image outputs not produced, unchanged, or unreadable: " + ", ".join(missing) + "]")
        if self.errors:
            lines.extend(f"[Image {path.name}: {error}]" for path, error in self.errors.items())
        return "\n".join(lines)
