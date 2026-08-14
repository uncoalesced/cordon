# Engineered by uncoalesced

from __future__ import annotations

import os
from pathlib import Path

import pytest

from features.adapters import get_adapter
from features.adapters import claude_agent_sdk as sdk_adapter
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    read_jsonl,
)

ADAPTER = sdk_adapter.ADAPTER

LIVE_PRE = {
    "session_id": "ef6e44fa-7998-46f9-9e65-5fc1766b892b",
    "transcript_path": "C:\\Users\\x\\.claude\\projects\\E--Cordon\\ef6e44fa.jsonl",
    "cwd": "E:\\Cordon",
    "prompt_id": "f169feb2-84ba-4316-8f78-5851f159b2a3",
    "permission_mode": "bypassPermissions",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "echo cordon-probe", "description": "Echo cordon-probe string"},
    "tool_use_id": "toolu_013XPXQeg7gTS1QeRUYF8qKF",
}

LIVE_POST = {
    **LIVE_PRE,
    "hook_event_name": "PostToolUse",
    "tool_response": {"stdout": "cordon-probe", "stderr": "", "interrupted": False, "isImage": False},
    "duration_ms": 2086,
}


def test_the_sdk_adapter_is_registered_as_live():
    assert get_adapter("claude-agent-sdk") is ADAPTER
    assert ADAPTER.verification == "live"


def test_a_real_captured_pre_payload_normalizes():
    normalized = ADAPTER.normalize(LIVE_PRE)
    assert normalized.event == EVENT_TOOL_START
    assert normalized.session_id == "ef6e44fa-7998-46f9-9e65-5fc1766b892b"
    assert normalized.tool_name == "Bash"
    assert normalized.tool_input["command"] == "echo cordon-probe"
    assert normalized.tool_use_id == "toolu_013XPXQeg7gTS1QeRUYF8qKF"


def test_a_real_captured_post_payload_normalizes_with_its_duration():
    normalized = ADAPTER.normalize(LIVE_POST)
    assert normalized.event == EVENT_TOOL_END
    assert normalized.reported_duration_ms == 2086.0
    assert normalized.tool_response["stdout"] == "cordon-probe"


def test_unknown_runtime_fields_are_ignored_not_rejected():
    normalized = ADAPTER.normalize({**LIVE_PRE, "some_field_shipped_next_week": 1})
    assert normalized.event == EVENT_TOOL_START


def test_the_sdk_has_no_session_start_so_stop_carries_the_end():
    assert "SessionStart" not in ADAPTER.events
    assert ADAPTER.events["Stop"] == EVENT_SESSION_END


def test_a_failed_tool_call_still_closes_its_window():
    assert ADAPTER.events["PostToolUseFailure"] == EVENT_TOOL_END


def test_the_sdk_adapter_writes_no_config_file():
    assert ADAPTER.writes_config is False
    assert ADAPTER.settings_relpath == ()


def test_observe_writes_a_marker_through_the_shared_core(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CORDON_DISABLE", "")
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)

    sdk_adapter.observe(dict(LIVE_PRE), run_root=tmp_path)
    sdk_adapter.observe(dict(LIVE_POST), run_root=tmp_path)

    markers = list(read_jsonl(tmp_path / LIVE_PRE["session_id"] / MARKERS_FILENAME))
    assert [m["event"] for m in markers] == [EVENT_TOOL_START, EVENT_TOOL_END]
    assert markers[0]["call_key"] == markers[1]["call_key"] == LIVE_PRE["tool_use_id"]
    assert markers[0]["adapter"] == "claude-agent-sdk"
    assert markers[0]["tool_type"] == "Bash"
    assert markers[1]["reported_duration_ms"] == 2086.0


def test_hook_matchers_cover_every_sdk_event():
    pytest.importorskip("claude_agent_sdk")
    matchers = sdk_adapter.hook_matchers()
    assert set(matchers) == set(sdk_adapter.HOOK_EVENTS)
    assert all(entry[0].hooks for entry in matchers.values())


@pytest.mark.integration
@pytest.mark.skipif(
    not (Path(os.path.expanduser("~")) / ".claude" / ".credentials.json").exists()
    and not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs real Claude credentials to run an agent",
)
def test_a_live_agent_run_lands_a_marker_in_cordon_schema(tmp_path: Path, monkeypatch):
    anyio = pytest.importorskip("anyio")
    sdk = pytest.importorskip("claude_agent_sdk")

    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)

    async def drive():
        options = sdk.ClaudeAgentOptions(
            model="claude-haiku-4-5-20251001",
            allowed_tools=["Bash"],
            permission_mode="bypassPermissions",
            max_turns=2,
            hooks=sdk_adapter.hook_matchers(run_root=tmp_path),
        )
        async for _ in sdk.query(
            prompt="Run exactly this bash command and nothing else: echo cordon-probe", options=options
        ):
            pass

    anyio.run(drive)

    markers = [m for path in tmp_path.rglob(MARKERS_FILENAME) for m in read_jsonl(path)]
    events = {m["event"] for m in markers}
    assert EVENT_TOOL_START in events and EVENT_TOOL_END in events
    assert all(m["adapter"] == "claude-agent-sdk" for m in markers)
    assert any(m["tool_type"] == "Bash" for m in markers)
