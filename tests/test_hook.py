# Engineered by uncoalesced

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from features.wrapper import hook as hook_module
from features.wrapper.hook import spawn_sampler as real_spawn_sampler
from features.wrapper.reduce import reduce_run
from features.wrapper.sampler import stop_file
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    JsonlWriter,
    Sample,
    read_jsonl,
)

SESSION = "sess-abc"


@pytest.fixture(autouse=True)
def no_real_sampler(monkeypatch):
    spawned: list[tuple[Path, int, float]] = []
    monkeypatch.setattr(
        hook_module,
        "spawn_sampler",
        lambda run_dir, agent_pid, interval: spawned.append((Path(run_dir), agent_pid, interval)),
    )
    return spawned


def _payload(event: str, **extra) -> dict:
    return {"hook_event_name": event, "session_id": SESSION, "cwd": "C:\\work", **extra}


def test_pre_tool_use_writes_a_start_marker(tmp_path: Path):
    marker = hook_module.handle(
        _payload("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}),
        run_root=tmp_path,
        now=1000.0,
    )
    assert marker.event == EVENT_TOOL_START
    assert marker.tool_type == "Bash"
    assert marker.command == "pytest -q"
    assert marker.ts == 1000.0
    assert marker.hook_overhead_ms >= 0

    rows = list(read_jsonl(tmp_path / SESSION / MARKERS_FILENAME))
    assert rows[0]["event"] == EVENT_TOOL_START


def test_pre_and_post_share_a_call_key(tmp_path: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "pytest"}}
    pre = hook_module.handle(_payload("PreToolUse", **payload), run_root=tmp_path)
    post = hook_module.handle(
        _payload("PostToolUse", **payload, tool_response={"exit_code": 0}), run_root=tmp_path
    )
    assert pre.call_key == post.call_key
    assert post.event == EVENT_TOOL_END
    assert post.exit_status == "0"


def test_tool_use_id_wins_when_present(tmp_path: Path):
    pre = hook_module.handle(
        _payload("PreToolUse", tool_name="Bash", tool_input={"command": "a"}, tool_use_id="toolu_9"),
        run_root=tmp_path,
    )
    assert pre.call_key == "toolu_9"


def test_session_start_spawns_the_sampler(tmp_path: Path, no_real_sampler):
    marker = hook_module.handle(_payload("SessionStart"), run_root=tmp_path)
    assert marker.event == EVENT_SESSION_START
    assert no_real_sampler and no_real_sampler[0][0] == tmp_path / SESSION


def test_session_end_stops_the_sampler(tmp_path: Path):
    marker = hook_module.handle(_payload("SessionEnd"), run_root=tmp_path)
    assert marker.event == EVENT_SESSION_END
    assert stop_file(tmp_path / SESSION).exists()


def test_unknown_event_is_ignored_without_writing(tmp_path: Path):
    assert hook_module.handle(_payload("PreCompact"), run_root=tmp_path) is None
    assert not (tmp_path / SESSION / MARKERS_FILENAME).exists()


def test_missing_session_id_still_records(tmp_path: Path):
    marker = hook_module.handle({"hook_event_name": "PreToolUse", "tool_name": "Read"}, run_root=tmp_path)
    assert marker.session_id == "unknown-session"


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"exit_code": 1}, "1"),
        ({"is_error": True}, "error"),
        ({"stdout": "hi"}, "ok"),
        (None, ""),
        ("plain", "ok"),
        ('{"exitCode": 0}', "0"),  # Cursor's tool_output arrives as a JSON string
        ({"llmContent": "x", "error": {"message": "boom"}}, "error"),  # Gemini AfterTool
    ],
)
def test_exit_status_extraction(response, expected):
    assert hook_module._exit_status(response) == expected


@pytest.mark.parametrize(
    "event,expected_marker_event",
    [
        ("SessionStart", EVENT_SESSION_START),
        ("on_session_start", EVENT_SESSION_START),
        ("sessionStart", EVENT_SESSION_START),
        ("PreToolUse", EVENT_TOOL_START),
        ("pre_tool_call", EVENT_TOOL_START),
        ("preToolUse", EVENT_TOOL_START),
        ("BeforeTool", EVENT_TOOL_START),
        ("PostToolUse", EVENT_TOOL_END),
        ("post_tool_call", EVENT_TOOL_END),
        ("postToolUse", EVENT_TOOL_END),
        ("postToolUseFailure", EVENT_TOOL_END),
        ("AfterTool", EVENT_TOOL_END),
        ("SessionEnd", EVENT_SESSION_END),
        ("Stop", EVENT_SESSION_END),
        ("on_session_end", EVENT_SESSION_END),
        ("sessionEnd", EVENT_SESSION_END),
        ("stop", EVENT_SESSION_END),
    ],
)
def test_every_agents_event_names_map_to_the_right_marker(tmp_path: Path, event, expected_marker_event):
    marker = hook_module.handle(
        _payload(event, tool_name="Bash", tool_input={"command": "x"}), run_root=tmp_path
    )
    assert marker.event == expected_marker_event


def test_session_id_falls_back_to_cursors_conversation_id(tmp_path: Path):
    marker = hook_module.handle(
        {"hook_event_name": "preToolUse", "conversation_id": "conv-1", "tool_name": "Shell", "tool_input": {}},
        run_root=tmp_path,
    )
    assert marker.session_id == "conv-1"


def test_tool_use_id_falls_back_to_hermes_extra_tool_call_id(tmp_path: Path):
    marker = hook_module.handle(
        _payload(
            "pre_tool_call",
            tool_name="terminal",
            tool_input={"command": "ls"},
            extra={"tool_call_id": "hermes-42"},
        ),
        run_root=tmp_path,
    )
    assert marker.call_key == "hermes-42"


def test_main_never_fails_the_agent_on_bad_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("this is not json"))
    assert hook_module.main() == 0


def test_main_never_fails_the_agent_on_handler_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload("PreToolUse", tool_name="Bash"))))
    monkeypatch.setattr(hook_module, "handle", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hook_module.main() == 0


def test_main_respects_the_disable_switch(monkeypatch):
    monkeypatch.setenv(hook_module.ENV_DISABLE, "1")
    assert hook_module.main() == 0


def test_default_run_root_honours_the_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(hook_module.ENV_RUN_ROOT, str(tmp_path))
    assert hook_module.default_run_root() == tmp_path
    monkeypatch.delenv(hook_module.ENV_RUN_ROOT)
    assert hook_module.default_run_root().name == "runs"


def test_interval_falls_back_when_env_is_garbage(monkeypatch):
    monkeypatch.setenv(hook_module.ENV_INTERVAL, "0.05")
    assert hook_module._interval() == 0.05
    monkeypatch.setenv(hook_module.ENV_INTERVAL, "fast please")
    assert hook_module._interval() == hook_module.DEFAULT_INTERVAL_S


def test_sampler_running_is_false_without_a_usable_pid_file(run_dir: Path):
    assert hook_module.sampler_running(run_dir) is False
    hook_module.sampler_pid_path(run_dir).write_text("not a pid", encoding="utf-8")
    assert hook_module.sampler_running(run_dir) is False
    hook_module.sampler_pid_path(run_dir).write_text(str(2**31 - 1), encoding="utf-8")
    assert hook_module.sampler_running(run_dir) is False


def test_sampler_running_is_true_for_a_live_pid(run_dir: Path):
    hook_module.sampler_pid_path(run_dir).write_text(str(os.getpid()), encoding="utf-8")
    assert hook_module.sampler_running(run_dir) is True


def test_spawn_sampler_records_pid_and_clears_stale_stop_file(run_dir: Path, monkeypatch):
    stop_file(run_dir).touch()
    monkeypatch.setattr(hook_module.subprocess, "Popen", lambda *_a, **_k: SimpleNamespace(pid=4242))

    assert real_spawn_sampler(run_dir, agent_pid=os.getpid(), interval=0.25) == 4242
    assert hook_module.sampler_pid_path(run_dir).read_text(encoding="utf-8") == "4242"
    assert not stop_file(run_dir).exists()


def test_spawn_sampler_is_a_noop_when_one_is_already_running(run_dir: Path):
    hook_module.sampler_pid_path(run_dir).write_text(str(os.getpid()), encoding="utf-8")
    assert real_spawn_sampler(run_dir, agent_pid=os.getpid(), interval=0.25) is None


def test_spawn_sampler_survives_a_failed_spawn(run_dir: Path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no exec for you")

    monkeypatch.setattr(hook_module.subprocess, "Popen", boom)
    assert real_spawn_sampler(run_dir, agent_pid=os.getpid(), interval=0.25) is None


@pytest.mark.integration
def test_full_cycle_hooks_then_reduce(tmp_path: Path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    hook_module.handle(_payload("SessionStart"), run_root=tmp_path, now=10.0)
    hook_module.handle(_payload("PreToolUse", **payload), run_root=tmp_path, now=11.0)
    hook_module.handle(_payload("PostToolUse", **payload, tool_response={"exit_code": 0}), run_root=tmp_path, now=14.0)
    hook_module.handle(_payload("SessionEnd"), run_root=tmp_path, now=15.0)

    run_dir = tmp_path / SESSION
    with JsonlWriter(run_dir / "samples.jsonl") as writer:
        for t, mem in [(10.5, 185.0), (11.5, 300.0), (12.5, 950.0), (13.5, 420.0), (14.5, 190.0)]:
            writer.write(Sample(t=t, mem_mb=mem, cpu_pct=25.0, n_procs=3))

    result = reduce_run(run_dir)

    assert result.n_toolcalls == 1
    assert result.unpaired_starts == 0 and result.orphan_ends == 0
    record = result.records[0]
    assert record.command == "pytest -q"
    assert record.peak_memory_mb == 950.0
    assert record.n_samples == 3
    assert record.avg_memory_mb == 556.667
    assert round(record.peak_memory_mb / record.avg_memory_mb, 2) == 1.71
