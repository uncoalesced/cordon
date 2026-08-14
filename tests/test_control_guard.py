# Engineered by uncoalesced

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from features.control.cgroup import CgroupHandle, CgroupStats, NullBackend
from features.control.guard import run_guarded
from features.control.intent import ENV_HINT, FeedbackPolicy
from features.wrapper.schema import read_jsonl

GB = 1024**3


class RecordingBackend(NullBackend):
    name = "recording"

    def __init__(self, stats: CgroupStats | None = None, attached: bool = True) -> None:
        super().__init__()
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.stats = stats or CgroupStats(observable=True)
        self.attached = attached
        self.joined = 0

    def create(self, name: str) -> CgroupHandle:
        self.created.append(name)
        return CgroupHandle(name=name, backend=self.name)

    def join_self(self, handle: CgroupHandle) -> None:
        self.joined += 1

    def confirm_membership(self, handle: CgroupHandle) -> bool:
        return self.attached

    def read_stats(self, handle: CgroupHandle) -> CgroupStats:
        return self.stats

    def destroy(self, handle: CgroupHandle) -> bool:
        self.destroyed.append(handle.name)
        return True


def _echo(text: str) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.stdout.write({text!r})"]


def _fail(code: int, message: str = "boom") -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.stderr.write({message!r}); sys.exit({code})"]


def test_a_successful_call_reports_its_return_code_and_cgroup():
    backend = RecordingBackend()
    result = run_guarded(_echo("hi"), backend=backend, env={}, interval=0.01)
    assert result.returncode == 0
    assert result.cgroup_name.startswith("tool_")
    assert backend.created == [result.cgroup_name]


def test_every_call_gets_its_own_cgroup_and_tears_it_down():
    backend = RecordingBackend()
    for _ in range(2):
        run_guarded(_echo("hi"), backend=backend, env={}, interval=0.01)
    assert len(backend.created) == 2
    assert backend.destroyed == backend.created


def test_the_cgroup_is_torn_down_even_when_the_command_fails():
    backend = RecordingBackend()
    result = run_guarded(_fail(3), backend=backend, env={}, interval=0.01)
    assert result.returncode == 3
    assert backend.destroyed == backend.created


def test_a_command_that_cannot_start_degrades_instead_of_raising():
    backend = RecordingBackend()
    result = run_guarded(["definitely-not-a-real-binary-xyz"], backend=backend, env={}, interval=0.01)
    assert result.returncode == 127
    assert result.error
    assert backend.destroyed == backend.created


def test_the_upward_hint_reaches_the_cgroup_limits():
    backend = RecordingBackend()
    result = run_guarded(_echo("hi"), hint="memory:high,cpu:low", backend=backend, env={}, interval=0.01)
    assert result.intent["memory_tier"] == "high"
    assert result.intent["cpu_weight"] == 25


def test_the_hint_is_read_from_the_environment_when_not_passed():
    backend = RecordingBackend()
    result = run_guarded(_echo("hi"), backend=backend, env={ENV_HINT: "memory:low"}, interval=0.01)
    assert result.intent["source"] == "env"
    assert result.intent["memory_tier"] == "low"


def test_an_explicit_hint_is_exported_to_the_child():
    backend = RecordingBackend()
    argv = [sys.executable, "-c", f"import os,sys; sys.stderr.write(os.environ['{ENV_HINT}'])"]
    result = run_guarded(argv, hint="memory:high", backend=backend, env={}, interval=0.01)
    assert result.returncode == 0


def test_downward_feedback_lands_on_stderr_when_the_call_was_throttled(capsys):
    backend = RecordingBackend(CgroupStats(peak_memory_mb=1842.0, memory_stall_s=1.5, observable=True))
    result = run_guarded(_fail(1, "pytest failed\n"), backend=backend, env={}, interval=0.01)

    captured = capsys.readouterr().err
    assert "pytest failed" in captured
    assert "[cordon]" in captured
    assert "1842.0 MB" in captured
    assert result.feedback


def test_no_feedback_when_nothing_was_throttled(capsys):
    backend = RecordingBackend(CgroupStats(peak_memory_mb=40.0, memory_stall_s=0.0, observable=True))
    result = run_guarded(_fail(1, "plain failure\n"), backend=backend, env={}, interval=0.01)

    captured = capsys.readouterr().err
    assert "plain failure" in captured
    assert "[cordon]" not in captured
    assert result.feedback == ""


def test_an_unattached_process_never_gets_feedback_about_limits_it_never_had(capsys):
    backend = RecordingBackend(
        CgroupStats(peak_memory_mb=1842.0, memory_stall_s=9.0, observable=True), attached=False
    )
    result = run_guarded(_echo("hi"), backend=backend, env={}, interval=0.01)
    assert result.attached is False
    assert result.feedback == ""
    assert "[cordon]" not in capsys.readouterr().err


def test_the_null_backend_runs_the_command_unchanged(capsys):
    result = run_guarded(_fail(2, "unchanged\n"), backend=NullBackend(), env={}, interval=0.01)
    assert result.returncode == 2
    assert result.backend == "null"
    assert result.feedback == ""
    assert "unchanged" in capsys.readouterr().err


def test_a_backend_that_raises_on_stats_does_not_take_the_call_down():
    class Exploding(RecordingBackend):
        def read_stats(self, handle: CgroupHandle) -> CgroupStats:
            raise RuntimeError("stat read blew up")

    result = run_guarded(_echo("hi"), backend=Exploding(), env={}, interval=0.01)
    assert result.returncode == 0


def test_a_backend_that_raises_on_setup_still_runs_the_command():
    class Exploding(RecordingBackend):
        def create(self, name: str) -> CgroupHandle:
            raise RuntimeError("cgroup create blew up")

    result = run_guarded(_echo("hi"), backend=Exploding(), env={}, interval=0.01)
    assert result.returncode == 0
    assert result.attached is False


def test_a_runaway_command_is_killed_at_the_timeout():
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    result = run_guarded(argv, backend=RecordingBackend(), env={}, interval=0.01, timeout=0.3)
    assert result.error == "timeout"
    assert result.duration_s < 10


def test_results_can_be_appended_to_a_run_log(tmp_path: Path):
    record = tmp_path / "control.jsonl"
    backend = RecordingBackend()
    for _ in range(2):
        run_guarded(_echo("hi"), backend=backend, env={}, interval=0.01, record_path=record)

    rows = list(read_jsonl(record))
    assert len(rows) == 2
    assert rows[0]["cgroup_name"].startswith("tool_")
    assert json.dumps(rows[0])


def test_feedback_policy_state_carries_across_calls_in_one_session(capsys):
    policy = FeedbackPolicy()
    backend = RecordingBackend(CgroupStats(peak_memory_mb=900.0, memory_stall_s=1.5, observable=True))
    argv = _fail(1, "same command\n")
    for _ in range(3):
        run_guarded(argv, backend=backend, policy=policy, env={}, interval=0.01)
    assert "warning number 3" in capsys.readouterr().err


@pytest.mark.integration
def test_a_real_child_is_measured_end_to_end():
    argv = [sys.executable, "-c", "import time; buf=bytearray(20*1024*1024); time.sleep(0.3)"]
    result = run_guarded(argv, backend=RecordingBackend(), env={}, interval=0.05)
    assert result.returncode == 0
    assert result.duration_s >= 0.3
