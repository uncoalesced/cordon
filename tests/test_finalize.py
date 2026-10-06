# Engineered by uncoalesced

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from features.wrapper import finalize as finalize_module
from features.wrapper import hook as hook_module
from features.wrapper.cli import main
from features.wrapper.sampler import stop_file, write_pid_file
from features.wrapper.schema import JsonlWriter, Sample


@pytest.fixture(autouse=True)
def no_spawns(monkeypatch):
    monkeypatch.setattr(hook_module, "spawn_sampler", lambda *_a, **_k: None)
    monkeypatch.setattr(hook_module, "spawn_finalize", lambda *_a, **_k: None)


def record_session(root: Path, session: str = "s1", base: float = 1000.0) -> Path:
    def payload(event, **extra):
        return {"hook_event_name": event, "session_id": session, **extra}

    call = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    hook_module.handle(payload("SessionStart"), run_root=root, now=base)
    hook_module.handle(payload("PreToolUse", **call), run_root=root, now=base + 1)
    hook_module.handle(payload("PostToolUse", **call, tool_response={"exit_code": 0}), run_root=root, now=base + 4)
    hook_module.handle(payload("SessionEnd"), run_root=root, now=base + 5)
    run_dir = root / session
    with JsonlWriter(run_dir / "samples.jsonl") as writer:
        for offset, mem in [(0.5, 185.0), (1.5, 300.0), (2.5, 950.0), (3.5, 420.0), (4.5, 190.0)]:
            writer.write(Sample(t=base + offset, mem_mb=mem, cpu_pct=20.0, n_procs=3, mem_mb_unique=mem / 2))
    return run_dir


def test_finalize_writes_a_report_with_the_paper_comparison(tmp_path: Path):
    run_dir = record_session(tmp_path)
    out = finalize_module.finalize(run_dir, wait_s=0.1)
    text = out.read_text(encoding="utf-8")
    assert out == run_dir / "report.md"
    assert "Comparison against the paper" in text
    assert "Memory metric for per-run figures: unique" in text
    assert "10th percentile" in text


def test_finalize_reports_even_if_the_sampler_will_not_exit(tmp_path: Path, monkeypatch):
    run_dir = record_session(tmp_path)
    monkeypatch.setattr(finalize_module, "sampler_running", lambda _d: True)
    assert finalize_module.wait_for_sampler(run_dir, timeout_s=0.05, poll_s=0.01) is False
    assert finalize_module.finalize(run_dir, wait_s=0.05).exists()


def test_finalize_returns_none_on_failure(tmp_path: Path, monkeypatch):
    run_dir = record_session(tmp_path)
    monkeypatch.setattr(finalize_module, "reduce_run", lambda _d: (_ for _ in ()).throw(RuntimeError("x")))
    assert finalize_module.finalize(run_dir, wait_s=0) is None
    assert main(["finalize", "--run-dir", str(run_dir), "--wait", "0"]) == 1


def test_report_last_generates_when_missing(tmp_path: Path, capsys):
    record_session(tmp_path, "old", base=1000.0)
    newest = record_session(tmp_path, "new", base=2000.0)
    os.utime(newest / "markers.jsonl", (5e9, 5e9))
    assert main(["report", "--last", "--runs", str(tmp_path)]) == 0
    assert "Cordon session report: new" in capsys.readouterr().out
    assert (newest / "report.md").exists()


def test_report_session_and_all(tmp_path: Path, capsys):
    record_session(tmp_path, "a")
    record_session(tmp_path, "b", base=3000.0)
    assert main(["report", "--session", "a", "--runs", str(tmp_path)]) == 0
    assert "Cordon session report: a" in capsys.readouterr().out
    assert main(["report", "--all", "--runs", str(tmp_path)]) == 0
    assert "2 run(s)" in capsys.readouterr().out


def test_report_errors_cleanly(tmp_path: Path, monkeypatch):
    assert main(["report", "--runs", str(tmp_path)]) == 1
    assert main(["report", "--session", "nope", "--runs", str(tmp_path)]) == 1
    record_session(tmp_path)
    monkeypatch.setattr(finalize_module, "write_run_report", lambda _d: (_ for _ in ()).throw(RuntimeError("x")))
    assert main(["report", "--runs", str(tmp_path)]) == 1


def test_report_defaults_to_the_hook_run_root(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv(hook_module.ENV_RUN_ROOT, str(tmp_path))
    record_session(tmp_path)
    assert main(["report"]) == 0
    assert "Comparison against the paper" in capsys.readouterr().out


def test_status_lists_runs(tmp_path: Path, capsys):
    run_dir = record_session(tmp_path)
    finalize_module.finalize(run_dir, wait_s=0)
    assert main(["status", "--runs", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    header, row = out.strip().splitlines()
    assert header.split()[:2] == ["session", "started"]
    assert row.split()[0] == "s1" and "5s" in row and "950" in row and "yes" in row


def test_status_with_no_runs(tmp_path: Path, capsys):
    assert main(["status", "--runs", str(tmp_path / "missing")]) == 0
    assert "no runs recorded" in capsys.readouterr().out


@pytest.fixture
def live_sampler():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "sample"])
    yield proc
    proc.kill()
    proc.wait(timeout=5)


def test_status_clean_stops_samplers_whose_agent_is_gone(tmp_path: Path, live_sampler, capsys):
    run_dir = record_session(tmp_path)
    write_pid_file(run_dir / hook_module.SAMPLER_PID_FILENAME, live_sampler.pid)
    (run_dir / "agent.pid").write_text(f"{2**31 - 1} 1.0", encoding="utf-8")
    stop_file(run_dir).unlink(missing_ok=True)
    assert main(["status", "--clean", "--runs", str(tmp_path)]) == 0
    assert "stopped orphaned sampler: s1" in capsys.readouterr().out
    assert stop_file(run_dir).exists()


def test_clean_leaves_an_active_session_alone(tmp_path: Path, live_sampler):
    run_dir = record_session(tmp_path)
    write_pid_file(run_dir / hook_module.SAMPLER_PID_FILENAME, live_sampler.pid)
    write_pid_file(run_dir / "agent.pid", os.getpid())
    stop_file(run_dir).unlink(missing_ok=True)
    assert finalize_module.clean_orphans(tmp_path) == []
    assert not stop_file(run_dir).exists()
