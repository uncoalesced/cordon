# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

from features.wrapper.schema import (
    JsonlWriter,
    Marker,
    Sample,
    ToolCallRecord,
    append_jsonl,
    call_key_for,
    read_jsonl,
    summarize_command,
)


def test_call_key_is_stable_for_equivalent_input():
    a = call_key_for("s1", "Bash", {"command": "pytest", "timeout": 5})
    b = call_key_for("s1", "Bash", {"timeout": 5, "command": "pytest"})
    assert a == b


def test_call_key_separates_sessions_and_tools():
    base = call_key_for("s1", "Bash", {"command": "pytest"})
    assert base != call_key_for("s2", "Bash", {"command": "pytest"})
    assert base != call_key_for("s1", "Read", {"command": "pytest"})
    assert base != call_key_for("s1", "Bash", {"command": "pytest -x"})


def test_call_key_prefers_tool_use_id():
    assert call_key_for("s1", "Bash", {"command": "pytest"}, tool_use_id="toolu_123") == "toolu_123"


def test_call_key_survives_unserializable_input():
    key = call_key_for("s1", "Bash", {"command": object()})
    assert len(key) == 16


def test_summarize_command_picks_the_meaningful_field():
    assert summarize_command("Bash", {"command": "pytest -q", "description": "run"}) == "pytest -q"
    assert summarize_command("Read", {"file_path": "a.py"}) == "a.py"
    assert summarize_command("Weird", {"blob": 1}) == '{"blob": 1}'
    assert summarize_command("Weird", "plain") == "plain"


def test_summarize_command_truncates():
    assert len(summarize_command("Bash", {"command": "x" * 5000}, limit=100)) == 100


def test_jsonl_roundtrip(run_dir: Path):
    path = run_dir / "out.jsonl"
    with JsonlWriter(path) as writer:
        writer.write(Sample(t=1.0, mem_mb=2.0, cpu_pct=3.0, n_procs=4))
        writer.write(Marker(event="tool_start", ts=2.0, tool_type="Bash"))

    rows = list(read_jsonl(path))
    assert Sample.from_dict(rows[0]) == Sample(t=1.0, mem_mb=2.0, cpu_pct=3.0, n_procs=4)
    assert Marker.from_dict(rows[1]).tool_type == "Bash"


def test_read_jsonl_skips_bad_lines_and_keeps_going(run_dir: Path):
    path = run_dir / "mixed.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n{"a": 2}\n', encoding="utf-8")
    assert [row["a"] for row in read_jsonl(path)] == [1, 2]


def test_read_jsonl_on_missing_file_yields_nothing(run_dir: Path):
    assert list(read_jsonl(run_dir / "absent.jsonl")) == []


def test_append_jsonl_creates_parents(run_dir: Path):
    path = run_dir / "nested" / "deep" / "out.jsonl"
    assert append_jsonl(path, {"ok": True}) is True
    assert list(read_jsonl(path)) == [{"ok": True}]


def test_toolcall_record_keeps_spec_field_names():
    record = ToolCallRecord(
        task_id="t", tool_type="Bash", command="pytest",
        start_ts=1.0, end_ts=2.0,
        peak_memory_mb=10.0, avg_memory_mb=5.0, avg_cpu_pct=1.0,
    )
    payload = record.to_dict()
    for key in (
        "task_id", "tool_type", "command", "start_ts", "end_ts",
        "peak_memory_mb", "avg_memory_mb", "avg_cpu_pct", "samples",
    ):
        assert key in payload
