# Engineered by uncoalesced

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

from cordon import host
from cordon.control.cgroup import select_backend
from cordon.control.guard import run_guarded
from cordon.control.intent import Intent, render_feedback
from cordon.control.probe import enforcement_tier, probe_windows_job
from cordon.control.windows_job import (
    CREATE_SUSPENDED,
    IDLE_PRIORITY_CLASS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JobObjectCpuRateControlInformation,
    JobObjectExtendedLimitInformation,
    JobObjectMemoryUsageInformation,
    WindowsJobBackend,
    priority_class,
    windows_weight,
)

MB = 1024 * 1024


class FakeWin32:
    """Stands in for kernel32: records what was set, serves scripted memory readings."""

    def __init__(self, readings: list[tuple[int, int]] | None = None) -> None:
        self.readings = list(readings or [])
        self.set_calls: list[tuple[int, int, ctypes.Structure]] = []
        self.assigned: list[tuple[int, int]] = []
        self.closed: list[int] = []
        self.next_job = 100

    def create_job(self) -> int:
        self.next_job += 1
        return self.next_job

    def set_info(self, job, cls, struct):
        self.set_calls.append((job, cls, type(struct).from_buffer_copy(struct)))

    def query_info(self, job, cls, struct):
        if cls == JobObjectMemoryUsageInformation and self.readings:
            current, peak = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
            struct.JobMemory, struct.PeakJobMemoryUsed = current, peak
            return True
        return cls == JobObjectExtendedLimitInformation

    def assign_and_resume(self, job, pid):
        self.assigned.append((job, pid))

    def close(self, handle):
        self.closed.append(handle)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.5
        return self.t


def _setup(hard: bool, readings: list[tuple[int, int]]):
    fake = FakeWin32(readings)
    backend = WindowsJobBackend(hard_memory=hard, win32=fake, clock=Clock())
    handle = backend.create("tool_1")
    backend.apply(handle, Intent(memory_high_bytes=200 * MB, cpu_weight=25))
    return fake, backend, handle


def _set(fake: FakeWin32, cls: int):
    return next(s for _, c, s in fake.set_calls if c == cls)


@pytest.mark.parametrize(("weight", "expected"), [(1, 1), (25, 3), (50, 4), (100, 5), (400, 7), (1000, 8), (10000, 9)])
def test_cpu_weight_maps_one_job_step_per_doubling(weight: int, expected: int):
    assert windows_weight(weight) == expected


def test_only_low_tiers_change_priority():
    assert priority_class(25) == IDLE_PRIORITY_CLASS
    assert priority_class(100) is None and priority_class(400) is None


def test_soft_mode_watches_memory_and_sets_cpu_weight():
    fake, backend, handle = _setup(hard=False, readings=[(150 * MB, 150 * MB), (300 * MB, 300 * MB), (300 * MB, 300 * MB)])
    limits = _set(fake, JobObjectExtendedLimitInformation)
    assert not limits.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_JOB_MEMORY
    assert limits.BasicLimitInformation.PriorityClass == IDLE_PRIORITY_CLASS
    assert _set(fake, JobObjectCpuRateControlInformation).Value == 3
    assert handle.applied["memory.high"] == f"watch:{200 * MB}"

    for _ in range(3):
        stats = backend.read_stats(handle)
    assert stats.high_events == 1 and stats.memory_stall_s == pytest.approx(0.5)
    assert stats.peak_memory_mb == 300.0 and stats.stall_source == "job" and stats.max_events == 0


def test_kernel_peak_catches_a_spike_between_polls():
    _, backend, handle = _setup(hard=False, readings=[(50 * MB, 450 * MB)])
    stats = backend.read_stats(handle)
    assert stats.high_events == 1 and stats.peak_memory_mb == 450.0


def test_hard_mode_caps_job_memory_and_reports_the_hit():
    fake, backend, handle = _setup(hard=True, readings=[(199 * MB, 200 * MB)])
    limits = _set(fake, JobObjectExtendedLimitInformation)
    assert limits.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_JOB_MEMORY
    assert limits.JobMemoryLimit == 200 * MB
    assert backend.read_stats(handle).max_events == 1


def test_command_starts_suspended_then_joins_and_resumes():
    fake, backend, handle = _setup(hard=False, readings=[(0, 0)])
    assert backend.creationflags(handle) == CREATE_SUSPENDED
    assert not backend.confirm_membership(handle)
    backend.bind_pid(handle, 4242)
    assert fake.assigned == [(101, 4242)] and backend.confirm_membership(handle)
    backend.destroy(handle)
    assert fake.closed == [101] and backend.read_stats(handle).observable is False


def test_usable_needs_windows_or_an_injected_api():
    assert WindowsJobBackend(win32=FakeWin32()).usable()
    if sys.platform != "win32":
        assert not WindowsJobBackend().usable()


def test_feedback_says_windows_ran_on_and_names_the_hard_cap():
    intent = Intent(memory_high_bytes=200 * MB)
    soft = render_feedback(intent, stall_s=2.0, duration_s=3.0, peak_memory_mb=500.0, stall_source="job")
    assert "Windows cannot throttle memory" in soft and "stalled" not in soft
    hard = render_feedback(intent, stall_s=0.0, duration_s=3.0, peak_memory_mb=207.0, stall_source="job", limit_hits=1)
    assert "hard memory cap" in hard


def test_probe_reports_job_tier_only_on_windows():
    cap = probe_windows_job()
    if host.OS != host.WINDOWS:
        assert not cap.available
        return
    assert cap.available and enforcement_tier([cap]) == "job"


ALLOCATOR = """
import sys, time
held = []
try:
    for _ in range(6):
        held.append(bytearray(50 * 1024 * 1024))
        time.sleep(0.1)
except MemoryError:
    sys.exit(3)
time.sleep(0.6)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows only")
@pytest.mark.parametrize("hard", [False, True])
def test_real_job_limits_a_300mb_allocator_at_150mb(tmp_path: Path, hard: bool):
    script = tmp_path / "alloc.py"
    script.write_text(ALLOCATOR, encoding="utf-8")
    backend = select_backend(hard_memory=hard)
    assert backend.name == "jobobject"

    result = run_guarded([sys.executable, str(script)], hint="memory:150m", backend=backend, stderr_passthrough=False)

    assert result.attached
    if hard:
        assert result.returncode == 3
        assert result.stats["peak_memory_mb"] < 160
        assert result.stats["max_events"] == 1 and "hard memory cap" in result.feedback
    else:
        assert result.returncode == 0
        assert result.stats["peak_memory_mb"] > 290
        assert result.stats["high_events"] >= 1 and "Windows cannot throttle" in result.feedback
