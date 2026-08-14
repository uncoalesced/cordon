# Engineered by uncoalesced

from __future__ import annotations

import pytest

from features.adapters import ADAPTERS, DEFAULT_ADAPTER, get_adapter
from features.adapters.base import (
    VERIFIED_DOCS,
    VERIFIED_LIVE,
    Adapter,
    command_hook_settings,
    merge_hook_settings,
    standard_extract,
)
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
)


def test_the_registry_holds_every_shipped_adapter():
    assert set(ADAPTERS) == {"claude-code", "claude-agent-sdk", "codex", "antigravity", "vscode"}


def test_the_default_tool_is_claude_code():
    assert get_adapter().name == DEFAULT_ADAPTER == "claude-code"


def test_lookup_is_case_and_whitespace_tolerant():
    assert get_adapter("  CODEX ").name == "codex"


def test_an_unknown_tool_names_the_known_ones():
    with pytest.raises(KeyError) as excinfo:
        get_adapter("cursor")
    assert "antigravity" in str(excinfo.value)


@pytest.mark.parametrize("adapter", ADAPTERS.values(), ids=lambda a: a.name)
def test_every_adapter_declares_its_verification_honestly(adapter: Adapter):
    assert adapter.verification in (VERIFIED_LIVE, VERIFIED_DOCS)
    assert adapter.description


@pytest.mark.parametrize("adapter", ADAPTERS.values(), ids=lambda a: a.name)
def test_every_unverified_adapter_carries_a_caveat(adapter: Adapter):
    if adapter.verification != VERIFIED_LIVE:
        assert adapter.caveat, f"{adapter.name} is unverified and must say why that matters"


@pytest.mark.parametrize("adapter", ADAPTERS.values(), ids=lambda a: a.name)
def test_every_adapter_maps_both_tool_events(adapter: Adapter):
    assert EVENT_TOOL_START in adapter.events.values()
    assert EVENT_TOOL_END in adapter.events.values()
    assert EVENT_SESSION_END in adapter.events.values()


def test_the_standard_extractor_reads_the_common_payload():
    normalized = standard_extract(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "sess",
            "cwd": "/repo",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_use_id": "toolu_1",
        }
    )
    assert normalized.native_event == "PreToolUse"
    assert normalized.session_id == "sess"
    assert normalized.tool_name == "Bash"
    assert normalized.tool_input == {"command": "pytest"}
    assert normalized.tool_use_id == "toolu_1"


def test_the_standard_extractor_survives_an_empty_payload():
    normalized = standard_extract({})
    assert normalized.native_event == ""
    assert normalized.tool_input == {}
    assert normalized.reported_duration_ms == 0.0


@pytest.mark.parametrize(
    "payload,expected",
    [({"duration_ms": 2086}, 2086.0), ({"durationMs": "12.5"}, 12.5), ({"duration_ms": "nope"}, 0.0), ({}, 0.0)],
)
def test_a_self_reported_duration_is_captured_when_offered(payload, expected):
    assert standard_extract(payload).reported_duration_ms == expected


def test_normalize_maps_native_events_onto_cordon_events():
    adapter = get_adapter("claude-code")
    for native, expected in (
        ("SessionStart", EVENT_SESSION_START),
        ("PreToolUse", EVENT_TOOL_START),
        ("PostToolUse", EVENT_TOOL_END),
        ("SessionEnd", EVENT_SESSION_END),
        ("Stop", EVENT_SESSION_END),
    ):
        assert adapter.normalize({"hook_event_name": native}).event == expected


def test_an_unrecognised_event_normalizes_to_nothing_rather_than_raising():
    assert get_adapter("claude-code").normalize({"hook_event_name": "Telemetry"}).event == ""


def test_command_settings_cover_every_requested_event():
    settings = command_hook_settings("cordon hook", ("PreToolUse", "PostToolUse"))
    assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse"}
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "cordon hook"


def test_merging_preserves_unrelated_settings_and_is_idempotent():
    existing = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "other"}]}]}}
    additions = command_hook_settings("cordon hook", ("PreToolUse",))
    once = merge_hook_settings(existing, additions)
    twice = merge_hook_settings(once, additions)
    assert once["model"] == "opus"
    assert len(once["hooks"]["PreToolUse"]) == 2
    assert once == twice


def test_settings_paths_are_built_from_the_adapter_relpath(tmp_path):
    assert get_adapter("codex").settings_path_for(tmp_path) == tmp_path / ".codex" / "hooks.json"


def test_only_the_sdk_declines_to_write_a_config():
    assert [a.name for a in ADAPTERS.values() if not a.writes_config] == ["claude-agent-sdk"]
