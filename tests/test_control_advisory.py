# Engineered by uncoalesced

from __future__ import annotations

import os
from pathlib import Path

import pytest

from features import host
from features.control import advisory as advisory_module
from features.control.advisory import AdvisoryBackend, nice_for_weight, tree_memory_bytes
from features.control.intent import FeedbackPolicy, resolve_intent

MB = 1024 * 1024
GB = 1024**3


@pytest.mark.parametrize(
    "weight,nice",
    [(10000, 0), (400, 0), (100, 0), (50, 5), (25, 10), (12, 15), (1, 19), (0, 19)],
)
def test_cpu_weight_maps_to_nice(weight: int, nice: int):
    assert nice_for_weight(weight) == nice


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _watched(readings: list[int | None], hint: str = "memory:100M,cpu:low", os_kind: str = host.LINUX):
    clock = _Clock()
    queue = list(readings)
    backend = AdvisoryBackend(os_kind=os_kind, memory_reader=lambda pid: queue.pop(0), clock=clock)
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent(hint, env={}, total_bytes=16 * GB))
    backend.bind_pid(handle, 4242)
    return backend, handle, clock


def test_watchdog_counts_transitions_peak_and_over_limit_seconds():
    readings = [50 * MB, 150 * MB, 180 * MB, 90 * MB, 200 * MB, 210 * MB, None]
    backend, handle, clock = _watched(readings)
    assert backend.confirm_membership(handle) is True

    stats = None
    for _ in range(6):
        stats = backend.read_stats(handle)
        clock.t += 0.5
    # over at t=0.5 and t=1.0 (0.5s counted when re-sampled at 1.0, 0.5s more at 1.5),
    # under at 1.5, over again at 2.0 and 2.5 (0.5s counted at 2.5).
    assert stats.high_events == 2
    assert stats.memory_stall_s == pytest.approx(1.5)
    assert stats.peak_memory_mb == pytest.approx(210.0)
    assert stats.stall_source == "watchdog"
    assert stats.observable is True

    after_exit = backend.read_stats(handle)
    assert after_exit == stats  # tree gone: keep what was seen, do not zero it


def test_watchdog_never_flags_an_unlimited_intent():
    backend, handle, clock = _watched([10 * GB, 10 * GB], hint="memory:max")
    backend.read_stats(handle)
    clock.t += 5
    stats = backend.read_stats(handle)
    assert stats.high_events == 0 and stats.memory_stall_s == 0.0


def test_watchdog_over_limit_time_produces_agent_feedback():
    backend, handle, clock = _watched([300 * MB] * 5)
    stats = None
    for _ in range(5):
        stats = backend.read_stats(handle)
        clock.t += 0.25
    intent = resolve_intent("memory:100M", env={}, total_bytes=16 * GB)
    message = FeedbackPolicy().evaluate(
        command="python big.py",
        intent=intent,
        stall_s=stats.memory_stall_s,
        duration_s=1.3,
        peak_memory_mb=stats.peak_memory_mb,
        stall_source=stats.stall_source,
    )
    assert message and "over its memory limit" in message and "300.0 MB" in message


def test_apply_records_nice_and_watchdog_limit():
    backend, handle, _ = _watched([1])
    assert handle.applied["nice"] == "10"
    assert handle.applied["memory.high"] == f"watchdog:{100 * MB}"
    assert "taskpolicy" not in handle.applied  # linux never uses taskpolicy


def test_darwin_low_cpu_runs_under_taskpolicy_background(tmp_path: Path):
    taskpolicy = tmp_path / "taskpolicy"
    taskpolicy.write_text("", encoding="utf-8")
    backend = AdvisoryBackend(os_kind=host.DARWIN, taskpolicy=str(taskpolicy), memory_reader=lambda pid: 0)
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("cpu:low", env={}, total_bytes=16 * GB))
    assert backend.wrap_argv(handle, ["make"]) == [str(taskpolicy), "-b", "--", "make"]

    normal = backend.create("tool_1_3")
    backend.apply(normal, resolve_intent("cpu:medium", env={}, total_bytes=16 * GB))
    assert backend.wrap_argv(normal, ["make"]) == ["make"]


def test_darwin_without_taskpolicy_binary_runs_unwrapped(tmp_path: Path):
    backend = AdvisoryBackend(os_kind=host.DARWIN, taskpolicy=str(tmp_path / "absent"), memory_reader=lambda pid: 0)
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("cpu:low", env={}, total_bytes=16 * GB))
    assert backend.wrap_argv(handle, ["make"]) == ["make"]


def test_join_self_only_ever_raises_niceness(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(advisory_module.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(advisory_module.os, "getpriority", lambda which, who: 3, raising=False)
    monkeypatch.setattr(advisory_module.os, "setpriority", lambda *a: calls.append(a), raising=False)
    backend, handle, _ = _watched([1])  # nice 10
    backend.join_self(handle)
    assert calls == [(0, 0, 10)]

    calls.clear()
    handle.applied["nice"] = "0"
    backend.join_self(handle)
    assert calls == []


def test_linux_pss_is_read_from_smaps_rollup(tmp_path: Path):
    (tmp_path / "77").mkdir()
    (tmp_path / "77" / "smaps_rollup").write_text(
        "00400000-7fff [rollup]\nRss:              2048 kB\nPss:              1024 kB\n", encoding="utf-8"
    )
    assert advisory_module._linux_pss(77, proc_root=tmp_path) == 1024 * 1024
    assert advisory_module._linux_pss(78, proc_root=tmp_path) is None


def test_tree_memory_of_this_process_is_positive():
    assert (tree_memory_bytes(os.getpid()) or 0) > 0


def test_tree_memory_of_a_dead_pid_is_none():
    assert tree_memory_bytes(2**22 + 12345) is None
