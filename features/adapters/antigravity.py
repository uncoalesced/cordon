# Engineered by uncoalesced

from __future__ import annotations

from typing import Any, Mapping

from features.adapters.base import (
    VERIFIED_DOCS,
    Adapter,
    NormalizedEvent,
    _duration_ms,
    _text,
)
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
)

HOOK_NAME = "cordon"
MATCHED_EVENTS = ("PreToolUse", "PostToolUse")
PLAIN_EVENTS = ("Stop",)

EVENTS = {
    "PreToolUse": EVENT_TOOL_START,
    "PostToolUse": EVENT_TOOL_END,
    "Stop": EVENT_SESSION_END,
}

CAVEAT = (
    "As of 2026-08-06 a controlled reproduction reports zero hook invocations in Antigravity "
    "IDE 2.1.1 and Antigravity 2.0 desktop 2.5.0, while the same configuration fires on every "
    "tool call in the agy CLI 1.1.10. Expect this adapter to work against the CLI and not to "
    "fire at all in the IDE. An empty marker log there is Antigravity's behaviour, not Cordon's."
)


def extract(payload: Mapping[str, Any]) -> NormalizedEvent:
    tool_call = payload.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, Mapping) else {}
    error = _text(payload.get("error"))
    return NormalizedEvent(
        native_event=_text(payload.get("hook_event_name") or payload.get("hookEventName")),
        session_id=_text(payload.get("conversationId")),
        tool_name=_text(tool_call.get("name")),
        tool_input=tool_call.get("args") or {},
        tool_use_id=_text(payload.get("stepIdx")),
        cwd=_first_workspace(payload.get("workspacePaths")),
        tool_response={"is_error": True, "error": error} if error else {},
        reported_duration_ms=_duration_ms(payload),
    )


def _first_workspace(paths: Any) -> str:
    if isinstance(paths, str):
        return paths
    if isinstance(paths, (list, tuple)) and paths:
        return _text(paths[0])
    return ""


def build_settings(command: str) -> dict[str, Any]:
    matched = {
        event: [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]
        for event in MATCHED_EVENTS
    }
    plain = {event: [{"type": "command", "command": command}] for event in PLAIN_EVENTS}
    return {HOOK_NAME: {"enabled": True, **matched, **plain}}


def merge(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.update(additions)
    return merged


ADAPTER = Adapter(
    name="antigravity",
    verification=VERIFIED_DOCS,
    description="Antigravity agy CLI, camelCase payload with the tool call nested under toolCall",
    events=EVENTS,
    settings_relpath=(".agents", "hooks.json"),
    extract=extract,
    build_settings=build_settings,
    merge=merge,
    caveat=CAVEAT,
)
