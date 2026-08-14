# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

from features.adapters import antigravity as ag
from features.adapters import get_adapter
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    read_jsonl,
)

ADAPTER = ag.ADAPTER

SPEC_PRE = {
    "hook_event_name": "PreToolUse",
    "conversationId": "conv-9",
    "stepIdx": 4,
    "toolCall": {"name": "run_command", "args": {"command": "pytest -q"}},
    "workspacePaths": ["/repo", "/other"],
    "transcriptPath": "/tmp/t.jsonl",
    "artifactDirectoryPath": "/tmp/artifacts",
    "modelName": "gemini-3-pro",
}

SPEC_POST = {**SPEC_PRE, "hook_event_name": "PostToolUse", "error": "command failed"}


def test_antigravity_is_registered_as_spec_only():
    assert get_adapter("antigravity") is ADAPTER
    assert ADAPTER.verification == "docs"


def test_the_ide_non_firing_caveat_is_carried_not_buried():
    assert "zero hook invocations" in ADAPTER.caveat
    assert "agy CLI" in ADAPTER.caveat


def test_the_camel_case_payload_is_flattened():
    normalized = ADAPTER.normalize(SPEC_PRE)
    assert normalized.event == EVENT_TOOL_START
    assert normalized.session_id == "conv-9"
    assert normalized.tool_name == "run_command"
    assert normalized.tool_input == {"command": "pytest -q"}
    assert normalized.tool_use_id == "4"


def test_the_first_workspace_path_becomes_cwd():
    assert ADAPTER.normalize(SPEC_PRE).cwd == "/repo"
    assert ADAPTER.normalize({**SPEC_PRE, "workspacePaths": "/single"}).cwd == "/single"
    assert ADAPTER.normalize({**SPEC_PRE, "workspacePaths": []}).cwd == ""


def test_a_reported_error_becomes_an_error_exit_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)
    from features.wrapper import hook as hook_module

    hook_module.handle(dict(SPEC_PRE), run_root=tmp_path, adapter=ADAPTER)
    hook_module.handle(dict(SPEC_POST), run_root=tmp_path, adapter=ADAPTER)

    markers = list(read_jsonl(tmp_path / "conv-9" / MARKERS_FILENAME))
    assert [m["event"] for m in markers] == [EVENT_TOOL_START, EVENT_TOOL_END]
    assert markers[0]["call_key"] == markers[1]["call_key"]
    assert markers[1]["exit_status"] == "error"
    assert markers[0]["adapter"] == "antigravity"


def test_a_clean_post_reads_as_ok(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("features.wrapper.hook.spawn_sampler", lambda *_a, **_k: None)
    from features.wrapper import hook as hook_module

    marker = hook_module.handle(
        {**SPEC_PRE, "hook_event_name": "PostToolUse"}, run_root=tmp_path, adapter=ADAPTER
    )
    assert marker.exit_status == "ok"


def test_a_missing_tool_call_object_degrades_to_empty_fields():
    normalized = ADAPTER.normalize({"hook_event_name": "PreToolUse", "toolCall": "not-an-object"})
    assert normalized.tool_name == ""
    assert normalized.tool_input == {}


def test_stop_closes_the_session():
    assert ADAPTER.normalize({"hook_event_name": "Stop"}).event == EVENT_SESSION_END


def test_settings_use_the_named_hook_object(tmp_path: Path):
    assert ADAPTER.settings_path_for(tmp_path) == tmp_path / ".agents" / "hooks.json"
    settings = ADAPTER.build_settings("cordon hook --tool antigravity")
    entry = settings[ag.HOOK_NAME]
    assert entry["enabled"] is True
    assert entry["PreToolUse"][0]["matcher"] == "*"
    assert entry["Stop"][0]["type"] == "command"


def test_merging_preserves_other_named_hooks():
    existing = {"someone-elses-hook": {"enabled": True}}
    merged = ADAPTER.merge(existing, ADAPTER.build_settings("cordon hook"))
    assert "someone-elses-hook" in merged
    assert ag.HOOK_NAME in merged


def test_merging_is_idempotent():
    additions = ADAPTER.build_settings("cordon hook")
    once = ADAPTER.merge({}, additions)
    assert ADAPTER.merge(once, additions) == once
