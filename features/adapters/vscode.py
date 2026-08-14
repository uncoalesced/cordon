# Engineered by uncoalesced

from __future__ import annotations

from typing import Any

from features.adapters.base import VERIFIED_DOCS, Adapter, command_hook_settings

CONFIG_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "Stop")

CAVEAT = (
    "VS Code documents differences in tool names and property casing versus Claude Code, so "
    "per-tool-type comparisons across the two are only valid once the bucket names are checked. "
    "Written to .github/hooks/cordon.json rather than .claude/settings.json so installing for "
    "VS Code does not silently also install for Claude Code in the same repo."
)


def build_settings(command: str) -> dict[str, Any]:
    return command_hook_settings(command, CONFIG_EVENTS)


ADAPTER = Adapter(
    name="vscode",
    verification=VERIFIED_DOCS,
    description="VS Code agent mode, Claude Code hook format",
    settings_relpath=(".github", "hooks", "cordon.json"),
    build_settings=build_settings,
    caveat=CAVEAT,
)
