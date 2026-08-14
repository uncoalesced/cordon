# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

from features.adapters import claude_code
from features.adapters import get_adapter
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    read_jsonl,
)

ADAPTER = claude_code.ADAPTER

PRE = {
    "hook_event_name": "PreToolUse",
    "session_id": "cc-sess",
    "cwd": "/repo",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q"},
    "tool_use_id": "toolu_9",
}

POST = {**PRE, "hook_event_name": "PostToolUse", "tool_response": {"is_error": True}}


def test_claude_code_remains_the_default_and_is_live():
    assert get_adapter() is ADAPTER
    assert ADAPTER.verification == "live"
    assert ADAPTER.caveat == ""


def test_the_lifecycle_events_are_unchanged_from_stage_one():
    assert claude_code.CONFIG_EVENTS == ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")


def test_every_event_maps_as_before():
    for native, expected in (
        ("SessionStart", EVENT_SESSION_START),
        ("PreToolUse", EVENT_TOOL_START),
        ("PostToolUse", EVENT_TOOL_END),
        ("SessionEnd", EVENT_SESSION_END),
        ("Stop", EVENT_SESSION_END),
    ):
        assert ADAPTER.normalize({"hook_event_name": native}).event == expected


def test_settings_still_land_in_the_claude_settings_file(tmp_path: Path):
    assert ADAPTER.settings_path_for(tmp_path) == tmp_path / ".claude" / "settings.json"


def test_a_full_cycle_writes_paired_markers_tagged_with_the_adapter(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)
    from features.wrapper import hook as hook_module

    hook_module.handle(dict(PRE), run_root=tmp_path, adapter=ADAPTER)
    hook_module.handle(dict(POST), run_root=tmp_path, adapter=ADAPTER)

    markers = list(read_jsonl(tmp_path / "cc-sess" / MARKERS_FILENAME))
    assert [m["event"] for m in markers] == [EVENT_TOOL_START, EVENT_TOOL_END]
    assert markers[0]["adapter"] == "claude-code"
    assert markers[1]["exit_status"] == "error"
