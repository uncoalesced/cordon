# Engineered by uncoalesced

from __future__ import annotations

from typing import Any

from features.adapters.base import VERIFIED_LIVE, Adapter, command_hook_settings

CONFIG_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")


def build_settings(command: str) -> dict[str, Any]:
    return command_hook_settings(command, CONFIG_EVENTS)


ADAPTER = Adapter(
    name="claude-code",
    verification=VERIFIED_LIVE,
    description="Claude Code CLI, command hooks reading JSON on stdin",
    settings_relpath=(".claude", "settings.json"),
    build_settings=build_settings,
)
