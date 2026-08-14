# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

from features.adapters import get_adapter
from features.adapters import vscode as vscode_adapter
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    read_jsonl,
)

ADAPTER = vscode_adapter.ADAPTER

SPEC_PRE = {
    "timestamp": "2026-08-14T10:00:00Z",
    "cwd": "/repo",
    "session_id": "vscode-sess-2",
    "hook_event_name": "PreToolUse",
    "transcript_path": "/tmp/t.jsonl",
    "tool_name": "runInTerminal",
    "tool_input": {"command": "pytest -q"},
    "tool_use_id": "vsc_7",
}

SPEC_POST = {**SPEC_PRE, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}}


def test_vscode_is_registered_as_spec_only():
    assert get_adapter("vscode") is ADAPTER
    assert ADAPTER.verification == "docs"


def test_the_tool_naming_caveat_is_carried():
    assert "casing" in ADAPTER.caveat
    assert ".claude/settings.json" in ADAPTER.caveat


def test_the_claude_code_shaped_payload_normalizes_unchanged():
    normalized = ADAPTER.normalize(SPEC_PRE)
    assert normalized.event == EVENT_TOOL_START
    assert normalized.session_id == "vscode-sess-2"
    assert normalized.tool_name == "runInTerminal"
    assert normalized.tool_use_id == "vsc_7"


def test_stop_rather_than_session_end_closes_a_vscode_session():
    assert ADAPTER.normalize({"hook_event_name": "Stop"}).event == EVENT_SESSION_END
    assert ADAPTER.normalize({"hook_event_name": "SessionStart"}).event == EVENT_SESSION_START


def test_config_goes_to_the_vscode_workspace_path_not_the_claude_one(tmp_path: Path):
    assert ADAPTER.settings_path_for(tmp_path) == tmp_path / ".github" / "hooks" / "cordon.json"


def test_installing_for_vscode_does_not_touch_the_claude_settings(tmp_path: Path):
    from features.wrapper.cli import main

    assert main(["install-hooks", "--target", str(tmp_path), "--tool", "vscode", "--write"]) == 0
    assert not (tmp_path / ".claude").exists()


def test_settings_cover_the_documented_vscode_events():
    settings = ADAPTER.build_settings("cordon hook --tool vscode")
    assert set(settings["hooks"]) == set(vscode_adapter.CONFIG_EVENTS)
    assert "Stop" in settings["hooks"]


def test_a_full_cycle_writes_paired_markers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)
    from features.wrapper import hook as hook_module

    hook_module.handle(dict(SPEC_PRE), run_root=tmp_path, adapter=ADAPTER)
    hook_module.handle(dict(SPEC_POST), run_root=tmp_path, adapter=ADAPTER)

    markers = list(read_jsonl(tmp_path / "vscode-sess-2" / MARKERS_FILENAME))
    assert [m["event"] for m in markers] == [EVENT_TOOL_START, EVENT_TOOL_END]
    assert markers[0]["adapter"] == "vscode"
    assert markers[1]["exit_status"] == "0"


def test_a_payload_missing_its_tool_use_id_still_pairs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)
    from features.wrapper import hook as hook_module

    pre = {k: v for k, v in SPEC_PRE.items() if k != "tool_use_id"}
    post = {**pre, "hook_event_name": "PostToolUse"}
    first = hook_module.handle(pre, run_root=tmp_path, adapter=ADAPTER)
    second = hook_module.handle(post, run_root=tmp_path, adapter=ADAPTER)
    assert first.call_key and first.call_key == second.call_key
