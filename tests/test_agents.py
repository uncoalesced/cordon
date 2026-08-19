# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

from features.wrapper import agents

COMMAND = '"cordon.exe" hook'


def test_settings_path_per_agent(tmp_path: Path):
    assert agents.settings_path(agents.CLAUDE_CODE, tmp_path) == tmp_path / ".claude" / "settings.json"
    assert agents.settings_path(agents.CODEX, tmp_path) == tmp_path / ".codex" / "hooks.json"
    assert agents.settings_path(agents.GEMINI, tmp_path) == tmp_path / ".gemini" / "settings.json"
    assert agents.settings_path(agents.CURSOR, tmp_path) == tmp_path / ".cursor" / "hooks.json"


def test_hermes_home_defaults_to_user_home():
    assert agents.hermes_home() == Path.home() / ".hermes"


def test_hermes_home_honours_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(agents.ENV_HERMES_HOME, str(tmp_path))
    assert agents.hermes_home() == tmp_path
    assert agents.settings_path(agents.HERMES, tmp_path) == tmp_path / "config.yaml"


def test_nested_settings_uses_agent_specific_event_names():
    codex = agents.nested_settings(agents.CODEX, COMMAND)
    assert set(codex["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse", "SessionEnd"}

    gemini = agents.nested_settings(agents.GEMINI, COMMAND)
    assert set(gemini["hooks"]) == {"SessionStart", "BeforeTool", "AfterTool", "SessionEnd"}
    assert gemini["hooks"]["BeforeTool"][0]["hooks"][0]["command"] == COMMAND


def test_merge_nested_is_idempotent_and_preserves_other_keys():
    additions = agents.nested_settings(agents.GEMINI, COMMAND)
    once = agents.merge_nested({"theme": "dark"}, additions)
    twice = agents.merge_nested(once, additions)
    assert once == twice
    assert once["theme"] == "dark"
    assert len(once["hooks"]["BeforeTool"]) == 1


def test_cursor_settings_is_a_flat_shape():
    settings = agents.cursor_settings(COMMAND)
    assert settings["version"] == 1
    assert settings["hooks"]["preToolUse"] == [{"command": COMMAND}]
    assert set(settings["hooks"]) == {"sessionStart", "preToolUse", "postToolUse", "postToolUseFailure", "sessionEnd"}


def test_merge_cursor_dedupes_by_command():
    additions = agents.cursor_settings(COMMAND)
    once = agents.merge_cursor({}, additions)
    twice = agents.merge_cursor(once, additions)
    assert once == twice
    assert once["hooks"]["preToolUse"] == [{"command": COMMAND}]


def test_merge_cursor_preserves_existing_hooks():
    existing = {"version": 1, "hooks": {"preToolUse": [{"command": "./other.sh"}]}}
    merged = agents.merge_cursor(existing, agents.cursor_settings(COMMAND))
    commands = {h["command"] for h in merged["hooks"]["preToolUse"]}
    assert commands == {"./other.sh", COMMAND}


def test_hermes_hooks_block_covers_lifecycle_events():
    block = agents.hermes_hooks_block(COMMAND)
    assert set(block) == {"on_session_start", "pre_tool_call", "post_tool_call", "on_session_end"}
    assert block["pre_tool_call"] == [{"command": COMMAND}]


def test_merge_hermes_preserves_unrelated_top_level_keys():
    existing = {"hooks_auto_accept": False, "hooks": {"pre_tool_call": [{"command": "./audit.sh"}]}}
    merged = agents.merge_hermes(existing, agents.hermes_hooks_block(COMMAND))
    assert merged["hooks_auto_accept"] is False
    commands = {h["command"] for h in merged["hooks"]["pre_tool_call"]}
    assert commands == {"./audit.sh", COMMAND}


def test_ensure_codex_feature_flag_appends_a_new_table_when_absent():
    rendered = agents.ensure_codex_feature_flag("")
    assert "[features]" in rendered
    assert "codex_hooks = true" in rendered


def test_ensure_codex_feature_flag_inserts_into_an_existing_table():
    rendered = agents.ensure_codex_feature_flag("[features]\nother_flag = true\n")
    assert "codex_hooks = true" in rendered
    assert "other_flag = true" in rendered


def test_ensure_codex_feature_flag_flips_an_existing_false_to_true():
    rendered = agents.ensure_codex_feature_flag("[features]\ncodex_hooks = false\n")
    assert rendered.count("codex_hooks") == 1
    assert "codex_hooks = true" in rendered


def test_ensure_codex_feature_flag_is_idempotent_once_true():
    once = agents.ensure_codex_feature_flag("")
    twice = agents.ensure_codex_feature_flag(once)
    assert once == twice


def test_ensure_codex_feature_flag_preserves_unrelated_tables():
    original = "[model]\nname = \"gpt\"\n\n[features]\ncodex_hooks = false\n"
    rendered = agents.ensure_codex_feature_flag(original)
    assert 'name = "gpt"' in rendered
    assert "codex_hooks = true" in rendered
