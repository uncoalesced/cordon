# Engineered by uncoalesced

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Claude Code, Codex CLI, Hermes Agent, Cursor CLI, and Gemini CLI each independently shipped
# a hook system that is a renamed copy of the same idea: matcher + command groups, JSON piped
# to a script on stdin carrying tool_name/tool_input/session_id/cwd, exit code 2 (or a decision
# field) to block. cordon/wrapper/hook.py absorbs the event-name and field-name renaming via
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

# Events past the four lifecycle moments. Claude Code reports a tool call that failed as
# PostToolUseFailure, not PostToolUse; without it every failed call was left without an end.
EXTRA_EVENTS: dict[str, tuple[str, ...]] = {CLAUDE_CODE: ("PostToolUseFailure",)}

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


SCOPE_PROJECT = "project"
SCOPE_USER = "user"
SCOPES = (SCOPE_PROJECT, SCOPE_USER)


def install_target(scope: str, target: Path | str = ".") -> Path:
    """Directory the agent config dirs hang off: the repo, or $HOME for user-global hooks.

    Every agent's user-level file mirrors its project-level one under the home directory
    (~/.claude/settings.json, ~/.codex/hooks.json, ~/.gemini/settings.json, ~/.cursor/hooks.json).
    """
    return Path.home() if scope == SCOPE_USER else Path(target)


def is_user_global(agent: str, scope: str) -> bool:
    # Hermes only reads ~/.hermes/config.yaml, so it is user-global whatever --scope says.
    return scope == SCOPE_USER or agent == HERMES


def hermes_home() -> Path:
    override = os.environ.get(ENV_HERMES_HOME)
    return Path(override) if override else Path.home() / ".hermes"


def codex_config_path(target: Path) -> Path:
    return Path(target) / ".codex" / "config.toml"


TRANSPORT_HTTP = "http"
TRANSPORT_COMMAND = "command"
TRANSPORTS = (TRANSPORT_HTTP, TRANSPORT_COMMAND)
# Agents whose hook system can POST events to a URL instead of running a command.
HTTP_AGENTS = {CLAUDE_CODE}
# A hook that cannot reach the daemon must never stall the agent for the 600 s default.
HTTP_HOOK_TIMEOUT_S = 5
ENSURE_TIMEOUT_S = 15


def nested_settings(agent: str, hook_command: str) -> dict[str, Any]:
    entry = {"matcher": "*", "hooks": [{"type": "command", "command": hook_command}]}
    events = (*NESTED_EVENTS[agent], *EXTRA_EVENTS.get(agent, ()))
    return {"hooks": {event: [dict(entry)] for event in events}}


def http_settings(agent: str, ensure_command: str, url: str) -> dict[str, Any]:
    """SessionStart starts/registers with the daemon; every other event is a loopback POST, no process."""
    start, *rest = NESTED_EVENTS[agent]
    hooks: dict[str, Any] = {
        start: [{"matcher": "*", "hooks": [{"type": "command", "command": ensure_command, "timeout": ENSURE_TIMEOUT_S}]}]
    }
    for event in (*rest, *EXTRA_EVENTS.get(agent, ())):
        hooks[event] = [{"matcher": "*", "hooks": [{"type": "http", "url": url, "timeout": HTTP_HOOK_TIMEOUT_S}]}]
    return {"hooks": hooks}


def is_cordon_hook(hook: Any) -> bool:
    """A hook entry Cordon installed: its command runs cordon, or it posts to the daemon's /hook."""
    from cordon.wrapper.schema import is_cordon_cmd

    if not isinstance(hook, dict):
        return False
    if hook.get("type") == "http":
        url = str(hook.get("url", ""))
        return url.startswith(("http://127.0.0.1:", "http://localhost:")) and "/hook" in url
    return is_cordon_cmd(str(hook.get("command", "")))


def merge_nested(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    """Add Cordon's hooks, replacing any it installed before (command <-> http) so nothing records twice."""
    merged = dict(existing)
    hooks = dict(merged.get("hooks") or {})
    for event, entries in additions["hooks"].items():
        current = []
        for group in hooks.get(event) or []:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not is_cordon_hook(h)]
                if not kept:
                    continue
                group = {**group, "hooks": kept}
            current.append(group)
        current.extend(entries)
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
