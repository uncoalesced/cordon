# Engineered by uncoalesced

from __future__ import annotations

import json
import os
from pathlib import Path

from features.wrapper.cli import HOOK_EVENTS, _merge_hooks, build_parser, hook_settings, main
from features.wrapper.schema import MARKERS_FILENAME, SAMPLES_FILENAME, JsonlWriter, Marker, Sample


def test_hook_settings_covers_every_lifecycle_event():
    settings = hook_settings()
    assert set(settings["hooks"]) == set(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        assert settings["hooks"][event][0]["hooks"][0]["command"].endswith("hook --tool claude-code")


def test_merge_hooks_preserves_unrelated_settings():
    existing = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "other"}]}]}}
    merged = _merge_hooks(existing, hook_settings())
    assert merged["model"] == "opus"
    assert len(merged["hooks"]["PreToolUse"]) == 2


def test_merge_hooks_is_idempotent():
    once = _merge_hooks({}, hook_settings())
    twice = _merge_hooks(once, hook_settings())
    assert once == twice


def test_install_hooks_dry_run_writes_nothing(tmp_path: Path, capsys):
    assert main(["install-hooks", "--target", str(tmp_path)]) == 0
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert "dry run" in capsys.readouterr().out


def test_install_hooks_write_creates_valid_settings(tmp_path: Path):
    assert main(["install-hooks", "--target", str(tmp_path), "--write"]) == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == set(HOOK_EVENTS)


def test_install_hooks_refuses_to_clobber_unreadable_settings(tmp_path: Path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{ broken", encoding="utf-8")
    assert main(["install-hooks", "--target", str(tmp_path), "--write"]) == 1
    assert settings_path.read_text(encoding="utf-8") == "{ broken"


def test_reduce_command_prints_summary(tmp_path: Path, capsys):
    run_dir = tmp_path / "run"
    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(Marker(event="tool_start", ts=1.0, session_id="s", call_key="k", tool_type="Bash"))
        writer.write(Marker(event="tool_end", ts=2.0, session_id="s", call_key="k"))
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        writer.write(Sample(t=1.5, mem_mb=42.0, cpu_pct=1.0))

    assert main(["reduce", "--run-dir", str(run_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["n_toolcalls"] == 1


def test_sample_command_writes_a_stream(tmp_path: Path):
    run_dir = tmp_path / "run"
    assert main(["sample", "--run-dir", str(run_dir), "--pid", str(os.getpid()), "--interval", "0.01", "--max-duration", "0.15"]) == 0
    assert (run_dir / SAMPLES_FILENAME).exists()


def test_analyze_on_an_empty_root_still_reports(tmp_path: Path, capsys):
    assert main(["analyze", "--runs", str(tmp_path)]) == 0
    assert "## No dataset yet" in capsys.readouterr().out


def test_analyze_writes_a_report_file(tmp_path: Path):
    out = tmp_path / "docs" / "findings.md"
    assert main(["analyze", "--runs", str(tmp_path / "runs"), "--out", str(out), "--title", "Batch 1"]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Batch 1")


def test_analyze_emits_json(tmp_path: Path, capsys):
    assert main(["analyze", "--runs", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["n_runs"] == 0


def test_analyze_reports_measured_numbers(tmp_path: Path, capsys):
    run_dir = tmp_path / "runs" / "sess"
    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(Marker(event="tool_start", ts=1.0, session_id="sess", call_key="k", tool_type="Bash", command="pytest -q"))
        writer.write(Marker(event="tool_end", ts=3.0, session_id="sess", call_key="k"))
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        for t, mem in [(0.0, 200.0), (1.5, 900.0), (2.5, 400.0), (4.0, 210.0)]:
            writer.write(Sample(t=t, mem_mb=mem, cpu_pct=20.0))

    assert main(["reduce", "--run-dir", str(run_dir)]) == 0
    capsys.readouterr()
    assert main(["analyze", "--runs", str(tmp_path / "runs"), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["n_runs"] == 1
    assert payload["n_toolcalls"] == 1
    assert payload["tool_types"][0]["tool_type"] == "Bash"
    assert payload["bash_categories"][0]["tool_type"] == "test"


def test_analyze_aborts_cleanly_when_loading_explodes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "features.wrapper.cli.load_dataset",
        lambda _root: (_ for _ in ()).throw(RuntimeError("disk on fire")),
    )
    assert main(["analyze", "--runs", str(tmp_path)]) == 1


def test_install_hooks_writes_each_tool_to_its_own_path(tmp_path: Path):
    for tool, relpath in (
        ("claude-code", Path(".claude") / "settings.json"),
        ("codex", Path(".codex") / "hooks.json"),
        ("antigravity", Path(".agents") / "hooks.json"),
        ("vscode", Path(".github") / "hooks" / "cordon.json"),
    ):
        assert main(["install-hooks", "--target", str(tmp_path), "--tool", tool, "--write"]) == 0
        assert json.loads((tmp_path / relpath).read_text(encoding="utf-8"))


def test_install_hooks_carries_the_tool_into_the_hook_command(tmp_path: Path):
    assert main(["install-hooks", "--target", str(tmp_path), "--tool", "codex", "--write"]) == 0
    written = (tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert "hook --tool codex" in written


def test_install_hooks_warns_loudly_for_an_unverified_tool(tmp_path: Path, capsys):
    assert main(["install-hooks", "--target", str(tmp_path), "--tool", "antigravity"]) == 0
    out = capsys.readouterr().out
    assert "UNVERIFIED ADAPTER (antigravity)" in out
    assert "zero hook invocations" in out


def test_install_hooks_stays_quiet_for_a_verified_tool(tmp_path: Path, capsys):
    assert main(["install-hooks", "--target", str(tmp_path), "--tool", "claude-code"]) == 0
    assert "UNVERIFIED" not in capsys.readouterr().out


def test_install_hooks_for_the_sdk_prints_a_snippet_and_writes_nothing(tmp_path: Path, capsys):
    assert main(["install-hooks", "--target", str(tmp_path), "--tool", "claude-agent-sdk", "--write"]) == 0
    assert list(tmp_path.iterdir()) == []
    assert "hook_matchers()" in capsys.readouterr().out


def test_install_hooks_is_idempotent_per_tool(tmp_path: Path):
    path = tmp_path / ".codex" / "hooks.json"
    main(["install-hooks", "--target", str(tmp_path), "--tool", "codex", "--write"])
    once = path.read_text(encoding="utf-8")
    main(["install-hooks", "--target", str(tmp_path), "--tool", "codex", "--write"])
    assert path.read_text(encoding="utf-8") == once


def test_install_hooks_refuses_an_unparseable_file_for_any_tool(tmp_path: Path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ broken", encoding="utf-8")
    assert main(["install-hooks", "--target", str(tmp_path), "--tool", "codex", "--write"]) == 1
    assert path.read_text(encoding="utf-8") == "{ broken"


def test_adapters_lists_every_tool_with_its_verification(capsys):
    assert main(["adapters"]) == 0
    out = capsys.readouterr().out
    for tool in ("claude-code", "claude-agent-sdk", "codex", "antigravity", "vscode"):
        assert tool in out
    assert "live" in out and "docs" in out


def test_adapters_emits_json(capsys):
    assert main(["adapters", "--json"]) == 0
    rows = {row["tool"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["claude-agent-sdk"]["verification"] == "live"
    assert rows["antigravity"]["verification"] == "docs"
    assert rows["claude-agent-sdk"]["config"] == "(in code)"


def test_parser_requires_a_subcommand(capsys):
    parser = build_parser()
    try:
        parser.parse_args([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit")
