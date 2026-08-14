# Engineered by uncoalesced

from __future__ import annotations

from typing import Any

from features.adapters.base import VERIFIED_DOCS, Adapter, command_hook_settings

CONFIG_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")

CAVEAT = (
    "Only local tools (Bash, MCP, apply_patch, local functions) fire PreToolUse/PostToolUse. "
    "Hosted tools such as WebSearch do not, so a Codex tool-time fraction has a denominator "
    "that excludes hosted calls and is not directly comparable to Claude Code's."
)


def build_settings(command: str) -> dict[str, Any]:
    return command_hook_settings(command, CONFIG_EVENTS)


ADAPTER = Adapter(
    name="codex",
    verification=VERIFIED_DOCS,
    description="Codex CLI, command hooks reading JSON on stdin",
    settings_relpath=(".codex", "hooks.json"),
    build_settings=build_settings,
    caveat=CAVEAT,
)
