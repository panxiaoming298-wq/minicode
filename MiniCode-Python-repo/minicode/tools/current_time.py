from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from minicode.tooling import ToolDefinition, ToolResult

_FORMATS = {"iso", "unix", "date", "time", "full"}
_FIXED_ZONES = {
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "PST": -8,
    "PDT": -7,
    "JST": 9,
    "CET": 1,
    "CEST": 2,
}


def _validate(input_data: dict) -> dict:
    timezone_name = input_data.get("timezone", "local")
    output_format = input_data.get("format", "full")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("timezone must be a non-empty string")
    if not isinstance(output_format, str) or output_format not in _FORMATS:
        raise ValueError("format must be one of: iso, unix, date, time, full")
    return {"timezone": timezone_name.strip(), "format": output_format}


def _resolve_timezone(name: str):
    normalized = name.strip()
    if normalized.lower() == "local":
        return datetime.now().astimezone().tzinfo, "local"
    if normalized.upper() in {"UTC", "GMT"}:
        return timezone.utc, "UTC"
    if normalized.upper() in _FIXED_ZONES:
        abbreviation = normalized.upper()
        return timezone(timedelta(hours=_FIXED_ZONES[abbreviation]), abbreviation), abbreviation
    return ZoneInfo(normalized), normalized


def _offset_text(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return "unknown"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _run(input_data: dict, context) -> ToolResult:
    timezone_name = str(input_data.get("timezone", "local"))
    output_format = str(input_data.get("format", "full"))
    try:
        zone, display_name = _resolve_timezone(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        return ToolResult(
            ok=False,
            output=(
                f"TIMEZONE_NOT_FOUND\n无法识别时区: {timezone_name}\n"
                "请使用 IANA 时区名（例如 Asia/Shanghai）、UTC 或 local。\n"
                f"原始错误: {error}"
            ),
        )

    now = datetime.now(zone)
    if output_format == "iso":
        output = now.isoformat()
    elif output_format == "unix":
        output = str(int(now.timestamp()))
    elif output_format == "date":
        output = now.strftime("%Y-%m-%d")
    elif output_format == "time":
        output = now.strftime("%H:%M:%S")
    else:
        output = "\n".join(
            [
                f"DATE: {now:%Y-%m-%d}",
                f"TIME: {now:%H:%M:%S}",
                f"WEEKDAY: {now:%A}",
                f"TIMEZONE: {display_name}",
                f"UTC_OFFSET: {_offset_text(now)}",
                f"ISO: {now.isoformat()}",
            ]
        )
    return ToolResult(ok=True, output=output)


current_time_tool = ToolDefinition(
    name="current_time",
    description=(
        "Read the current date and time from the local system clock without network access or shell commands. "
        "Use this for questions about today's date, the current time, or weekdays. Supports local, UTC, and "
        "IANA zones such as Asia/Shanghai."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "local, UTC, or an IANA timezone such as Asia/Shanghai (default: local)",
            },
            "format": {
                "type": "string",
                "enum": ["iso", "unix", "date", "time", "full"],
                "description": "Output format (default: full)",
            },
        },
    },
    validator=_validate,
    run=_run,
)
