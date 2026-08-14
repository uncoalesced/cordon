# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

from features.adapters import codex as codex_adapter
from features.adapters import get_adapter
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    read_jsonl,
)

ADAPTER = codex_adapter.ADAPTER

SPEC_PRE = {
    "session_id": "codex-sess-1",
    "transcript_path": "/tmp/codex.jsonl",
    "cwd": "/repo",
    "hook_event_name": "PreToolUse",
    "model": "gpt-5-codex",
    "permission_mode": "default",
    "turn_id": "turn-7",
    "tool_name": "Bash",
    "tool_use_id": "call_42",
    "tool_input": {"command": "pytest -q"},
}

SPEC_POST = {**SPEC_PRE, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 1}}


def test_codex_is_registered_as_spec_only():
    assert get_adapter("codex") is ADAPTER
    assert ADAPTER.verification == "docs"


def test_the_hosted_tool_blind_spot_is_stated_in_the_caveat():
    assert "hosted" in ADAPTER.caveat.lower()
    assert "WebSearch" in ADAPTER.caveat


def test_the_spec_payload_normalizes_through_the_standard_extractor():
    normalized = ADAPTER.normalize(SPEC_PRE)
    assert normalized.event == EVENT_TOOL_START
    assert normalized.session_id == "codex-sess-1"
    assert normalized.tool_name == "Bash"
    assert normalized.tool_use_id == "call_42"
    assert normalized.cwd == "/repo"


def test_codex_specific_fields_do_not_break_extraction():
    assert ADAPTER.normalize(SPEC_PRE).tool_input == {"command": "pytest -q"}


def test_every_lifecycle_event_maps():
    for native, expected in (
        ("SessionStart", EVENT_SESSION_START),
        ("PreToolUse", EVENT_TOOL_START),
        ("PostToolUse", EVENT_TOOL_END),
        ("SessionEnd", EVENT_SESSION_END),
        ("Stop", EVENT_SESSION_END),
    ):
        assert ADAPTER.normalize({"hook_event_name": native}).event == expected


def test_settings_land_in_the_codex_hooks_file(tmp_path: Path):
    assert ADAPTER.settings_path_for(tmp_path) == tmp_path / ".codex" / "hooks.json"
    settings = ADAPTER.build_settings("cordon hook --tool codex")
    assert set(settings["hooks"]) == set(codex_adapter.CONFIG_EVENTS)


def test_a_full_pre_then_post_cycle_writes_paired_markers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)
    from features.wrapper import hook as hook_module

    hook_module.handle(dict(SPEC_PRE), run_root=tmp_path, adapter=ADAPTER)
    hook_module.handle(dict(SPEC_POST), run_root=tmp_path, adapter=ADAPTER)

    markers = list(read_jsonl(tmp_path / "codex-sess-1" / MARKERS_FILENAME))
    assert [m["event"] for m in markers] == [EVENT_TOOL_START, EVENT_TOOL_END]
    assert markers[0]["call_key"] == markers[1]["call_key"]
    assert markers[0]["adapter"] == "codex"
    assert markers[1]["exit_status"] == "1"


def test_a_malformed_payload_degrades_rather_than_raising():
    assert ADAPTER.normalize({"hook_event_name": "PreToolUse", "tool_input": None}).tool_input == {}
    assert ADAPTER.normalize({}).event == ""
