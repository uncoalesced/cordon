# Engineered by uncoalesced

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Claude Code, Codex CLI, Hermes Agent, Cursor CLI, and Gemini CLI each independently shipped
# a hook system that is a renamed copy of the same idea: matcher + command groups, JSON piped
# to a script on stdin carrying tool_name/tool_input/session_id/cwd, exit code 2 (or a decision
# field) to block. features/wrapper/hook.py absorbs the event-name and field-name renaming via
# alias tables. This module only needs to know, per agent, where its config file lives and what
# shape that file wants on disk.

CLAUDE_CODE = "claude-code"
CODEX = "codex"
GEMINI = "gemini"
CURSOR = "cursor"
HERMES = "hermes"

AGENT_CHOICES = (CLAUDE_CODE, CODEX, GEMINI, CURSOR, HERMES)

ENV_HERMES_HOME = "CORDON_HERMES_HOME"

# Claude Code, Codex, and Gemini CLI all use the same nested {"hooks": {event: [{"matcher":
# ..., "hooks": [{"type": "command", "command": ...}]}]}} shape - only the event names differ.
NESTED_EVENTS: dict[str, tuple[str, str, str, str]] = {
    CLAUDE_CODE: ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd"),
    CODEX: ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd"),
    GEMINI: ("SessionStart", "BeforeTool", "AfterTool", "SessionEnd"),
}

CURSOR_EVENTS = ("sessionStart", "preToolUse", "postToolUse", "postToolUseFailure", "sessionEnd")
HERMES_EVENTS = ("on_session_start", "pre_tool_call", "post_tool_call", "on_session_end")


def settings_path(agent: str, target: Path) -> Path:
    target = Path(target)
    if agent in NESTED_EVENTS:
        subdir = {CLAUDE_CODE: ".claude", CODEX: ".codex", GEMINI: ".gemini"}[agent]
        filename = "hooks.json" if agent == CODEX else "settings.json"
        return target / subdir / filename
    if agent == CURSOR:
        return target / ".cursor" / "hooks.json"
    if agent == HERMES:
        return hermes_home() / "config.yaml"
    raise ValueError(f"unknown agent {agent!r}")


def hermes_home() -> Path:
    override = os.environ.get(ENV_HERMES_HOME)
    return Path(override) if override else Path.home() / ".hermes"


def codex_config_path(target: Path) -> Path:
    return Path(target) / ".codex" / "config.toml"


def nested_settings(agent: str, hook_command: str) -> dict[str, Any]:
    entry = {"matcher": "*", "hooks": [{"type": "command", "command": hook_command}]}
    return {"hooks": {event: [dict(entry)] for event in NESTED_EVENTS[agent]}}


def merge_nested(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    hooks = dict(merged.get("hooks") or {})
    for event, entries in additions["hooks"].items():
        current = list(hooks.get(event) or [])
        commands = {
            h.get("command")
            for group in current
            if isinstance(group, dict)
            for h in (group.get("hooks") or [])
            if isinstance(h, dict)
        }
        for entry in entries:
            if entry["hooks"][0]["command"] not in commands:
                current.append(entry)
        hooks[event] = current
    merged["hooks"] = hooks
    return merged


def cursor_settings(hook_command: str) -> dict[str, Any]:
    return {"version": 1, "hooks": {event: [{"command": hook_command}] for event in CURSOR_EVENTS}}


def merge_cursor(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged.setdefault("version", additions["version"])
    hooks = dict(merged.get("hooks") or {})
    for event, entries in additions["hooks"].items():
        current = list(hooks.get(event) or [])
        commands = {h.get("command") for h in current if isinstance(h, dict)}
        for entry in entries:
            if entry["command"] not in commands:
                current.append(entry)
        hooks[event] = current
    merged["hooks"] = hooks
    return merged


def hermes_hooks_block(hook_command: str) -> dict[str, Any]:
    entry = {"command": hook_command}
    return {event: [dict(entry)] for event in HERMES_EVENTS}


def merge_hermes(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    hooks = dict(merged.get("hooks") or {})
    for event, entries in additions.items():
        current = list(hooks.get(event) or [])
        commands = {h.get("command") for h in current if isinstance(h, dict)}
        for entry in entries:
            if entry["command"] not in commands:
                current.append(entry)
        hooks[event] = current
    merged["hooks"] = hooks
    return merged


CODEX_FEATURE_LINE = "codex_hooks = true"


def ensure_codex_feature_flag(text: str) -> str:
    # ponytail: line-based patch, not a real TOML writer. Handles the common single-line
    # `codex_hooks = true|false` case inside a `[features]` table; an inline `features = {...}`
    # table or an array-of-tables named [[features]] would not be detected. Reach for a TOML
    # library here if either shows up in a real config.toml.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "[features]":
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith("["):
            if lines[j].strip().startswith("codex_hooks"):
                lines[j] = CODEX_FEATURE_LINE
                return "\n".join(lines) + "\n"
            j += 1
        lines.insert(i + 1, CODEX_FEATURE_LINE)
        return "\n".join(lines) + "\n"

    prefix = text if not text or text.endswith("\n") else text + "\n"
    return f"{prefix}\n[features]\n{CODEX_FEATURE_LINE}\n"
