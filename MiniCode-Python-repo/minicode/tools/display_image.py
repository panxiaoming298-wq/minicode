"""Publish an existing local raster image as a durable user-visible attachment."""
import json

from minicode.browser_capture import read_capture_metadata
from minicode.run_images import publish_image, read_image_file
from minicode.tooling import ToolDefinition, ToolMetadata, ToolCapability, ToolResult
from minicode.runtime_protocol import ToolEffect, ReplayPolicy
from minicode.tools._validation import validate_contract
from minicode.workspace import resolve_tool_path

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "path": {"type": "string", "minLength": 1, "description": "Path of an existing PNG, JPEG, WebP or static GIF, relative to the workspace or absolute."},
    "caption": {"type": "string", "maxLength": 500, "description": "Short user-facing description of this screenshot."},
}, "required": ["path"]}


def validate(value):
    validate_contract(value, SCHEMA)
    return value


def run(value, context):
    path = resolve_tool_path(context, value["path"], "read")
    try:
        data = read_image_file(path)
        record = publish_image(context, data, name=path.name,
                               caption=value.get("caption", ""), tool_name="display_image",
                               capture=read_capture_metadata(path, data))
        return ToolResult(ok=True, output=json.dumps({"image": record,
            "note": "Image saved and shown to the user. This tool does not inspect image contents."}, ensure_ascii=False))
    except (OSError, ValueError) as error:
        return ToolResult(ok=False, output=f"Cannot display image: {error}", failureKind="unsupported_content")


display_image_tool = ToolDefinition(name="display_image", description=(
    "Show a local screenshot/image to the user with a thumbnail, full-size preview and download. "
    "Copies verified image bytes into this session so they survive file changes, failures and cleanup. "
    "This tool does not visually inspect the image or certify UI correctness. "
    "For screenshots produced by a command, prefer run_command.image_paths to publish them while the command runs."),
    input_schema=SCHEMA, validator=validate, run=run,
    metadata=ToolMetadata(name="display_image", description="Show a saved image to the user",
        capabilities={ToolCapability.READ_ONLY}, effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.NEVER_AUTO_RETRY))
