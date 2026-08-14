# Engineered by uncoalesced

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from features.wrapper.logging_setup import get_logger
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
)

VERIFIED_LIVE = "live"
VERIFIED_DOCS = "docs"

STANDARD_EVENTS: Mapping[str, str] = {
    "SessionStart": EVENT_SESSION_START,
    "PreToolUse": EVENT_TOOL_START,
    "PostToolUse": EVENT_TOOL_END,
    "SessionEnd": EVENT_SESSION_END,
    "Stop": EVENT_SESSION_END,
}


@dataclass
class NormalizedEvent:
    native_event: str = ""
    event: str = ""
    session_id: str = ""
    tool_name: str = ""
    tool_input: Any = None
    tool_use_id: str = ""
    cwd: str = ""
    tool_response: Any = None
    reported_duration_ms: float = 0.0


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _duration_ms(payload: Mapping[str, Any]) -> float:
    for key in ("duration_ms", "durationMs"):
        try:
            return float(payload[key])
        except (KeyError, TypeError, ValueError):
            continue
    return 0.0


def standard_extract(payload: Mapping[str, Any]) -> NormalizedEvent:
    return NormalizedEvent(
        native_event=_text(payload.get("hook_event_name")),
        session_id=_text(payload.get("session_id")),
        tool_name=_text(payload.get("tool_name")),
        tool_input=payload.get("tool_input") or {},
        tool_use_id=_text(payload.get("tool_use_id")),
        cwd=_text(payload.get("cwd")),
        tool_response=payload.get("tool_response"),
        reported_duration_ms=_duration_ms(payload),
    )


def command_hook_settings(command: str, events: Iterable[str]) -> dict[str, Any]:
    entry = {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
    return {"hooks": {event: [dict(entry)] for event in events}}


def merge_hook_settings(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    hooks = dict(merged.get("hooks") or {})
    for event, entries in (additions.get("hooks") or {}).items():
        current = list(hooks.get(event) or [])
        commands = {
            handler.get("command")
            for group in current
            if isinstance(group, dict)
            for handler in (group.get("hooks") or [])
            if isinstance(handler, dict)
        }
        for entry in entries:
            if entry["hooks"][0]["command"] not in commands:
                current.append(entry)
        hooks[event] = current
    merged["hooks"] = hooks
    return merged


@dataclass
class Adapter:
    name: str
    verification: str
    description: str = ""
    events: Mapping[str, str] = field(default_factory=lambda: dict(STANDARD_EVENTS))
    settings_relpath: tuple[str, ...] = ()
    extract: Callable[[Mapping[str, Any]], NormalizedEvent] = standard_extract
    build_settings: Callable[[str], dict[str, Any]] | None = None
    merge: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = merge_hook_settings
    caveat: str = ""

    @property
    def writes_config(self) -> bool:
        return bool(self.settings_relpath) and self.build_settings is not None

    def normalize(self, payload: Mapping[str, Any]) -> NormalizedEvent:
        normalized = self.extract(payload)
        normalized.event = self.events.get(normalized.native_event, "")
        if not normalized.event:
            get_logger("adapter").warning(
                "ignoring unrecognised hook event | adapter=%s event=%r session=%s",
                self.name,
                normalized.native_event,
                normalized.session_id,
            )
        return normalized

    def settings_path_for(self, target: Any) -> Any:
        from pathlib import Path

        path = Path(target)
        for part in self.settings_relpath:
            path = path / part
        return path
