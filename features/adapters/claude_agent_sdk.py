# Engineered by uncoalesced

from __future__ import annotations

from typing import Any

from features.adapters.base import VERIFIED_LIVE, Adapter
from features.wrapper.schema import EVENT_SESSION_END, EVENT_TOOL_END, EVENT_TOOL_START

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")

EVENTS = {
    "PreToolUse": EVENT_TOOL_START,
    "PostToolUse": EVENT_TOOL_END,
    "PostToolUseFailure": EVENT_TOOL_END,
    "Stop": EVENT_SESSION_END,
}

CAVEAT = (
    "The SDK has no SessionStart event, so the sampler starts on the first PreToolUse. Hooks are "
    "in-process async callbacks rather than subprocesses, so there is no config file to install; "
    "call hook_matchers() and pass the result to ClaudeAgentOptions(hooks=...)."
)

ADAPTER = Adapter(
    name="claude-agent-sdk",
    verification=VERIFIED_LIVE,
    description="Claude Agent SDK, in-process async hook callbacks",
    events=EVENTS,
    caveat=CAVEAT,
)


def observe(payload: dict[str, Any], run_root: Any = None) -> Any:
    from features.wrapper import hook as hook_module

    return hook_module.handle(payload, adapter=ADAPTER, run_root=run_root)


def hook_matchers(run_root: Any = None, events: tuple[str, ...] = HOOK_EVENTS) -> dict[str, list[Any]]:
    from claude_agent_sdk import HookMatcher

    from features.wrapper.logging_setup import get_logger, log_failure

    log = get_logger("adapter.sdk")

    async def callback(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        try:
            observe(dict(input_data), run_root=run_root)
        except Exception:
            log_failure(log, "sdk hook observation failed, agent unaffected", payload=repr(input_data)[:500])
        return {}

    log.info("sdk hook matchers built | events=%s", list(events))
    return {event: [HookMatcher(hooks=[callback])] for event in events}
