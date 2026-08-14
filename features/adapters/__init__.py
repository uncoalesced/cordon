# Engineered by uncoalesced

from __future__ import annotations

from features.adapters import antigravity, claude_agent_sdk, claude_code, codex, vscode
from features.adapters.base import Adapter

DEFAULT_ADAPTER = "claude-code"

ADAPTERS: dict[str, Adapter] = {
    adapter.name: adapter
    for adapter in (
        claude_code.ADAPTER,
        claude_agent_sdk.ADAPTER,
        codex.ADAPTER,
        antigravity.ADAPTER,
        vscode.ADAPTER,
    )
}


def get_adapter(name: str | None = None) -> Adapter:
    key = (name or DEFAULT_ADAPTER).strip().lower()
    if key not in ADAPTERS:
        raise KeyError(f"unknown tool {key!r}; known tools are {', '.join(sorted(ADAPTERS))}")
    return ADAPTERS[key]
