# Engineered by uncoalesced

from __future__ import annotations

import os
from pathlib import Path

import pytest

from features.control import cgroup as cgroup_module
from features.control.cgroup import (
    Cgroup2Backend,
    NullBackend,
    call_cgroup_name,
    select_backend,
)
from features.control.intent import resolve_intent

GB = 1024**3


@pytest.fixture
def fake_cgroupfs(tmp_path: Path) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n", encoding="utf-8")
    (root / "cgroup.subtree_control").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def backend(fake_cgroupfs: Path) -> Cgroup2Backend:
    return Cgroup2Backend(root=fake_cgroupfs)


def test_call_names_follow_the_paper_layout():
    assert call_cgroup_name(pid=48213, ts=1755000000.9) == "tool_48213_1755000000"


def test_availability_needs_both_controllers(tmp_path: Path):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpuset io pids\n", encoding="utf-8")
    assert Cgroup2Backend(root=root).available() is False


def test_availability_on_a_real_shaped_mount(backend: Cgroup2Backend):
    assert backend.available() is True


def test_create_delegates_controllers_down_the_tree(backend: Cgroup2Backend, fake_cgroupfs: Path):
    handle = backend.create("tool_1_2")
    assert handle.path is not None and handle.path.is_dir()
    assert (fake_cgroupfs / "cgroup.subtree_control").read_text(encoding="utf-8") == "+cpu +memory"
    assert (backend.parent / "cgroup.subtree_control").read_text(encoding="utf-8") == "+cpu +memory"


def test_create_is_idempotent(backend: Cgroup2Backend):
    first = backend.create("tool_1_2")
    second = backend.create("tool_1_2")
    assert first.path == second.path


def test_apply_writes_the_declared_limits(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("memory:high,cpu:low", env={}, total_bytes=16 * GB))
    assert (handle.path / "memory.high").read_text(encoding="utf-8") == str(int(16 * GB * 0.35))
    assert (handle.path / "cpu.weight").read_text(encoding="utf-8") == "25"
    assert (handle.path / "memory.oom.group").read_text(encoding="utf-8") == "1"


def test_max_tier_writes_the_literal_max_sentinel(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("memory:max", env={}, total_bytes=16 * GB))
    assert (handle.path / "memory.high").read_text(encoding="utf-8") == "max"


def test_join_self_writes_the_calling_pid(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    backend.join_self(handle)
    assert (handle.path / "cgroup.procs").read_text(encoding="utf-8") == str(os.getpid())
    assert backend.confirm_membership(handle) is True


def test_membership_is_false_while_the_cgroup_is_empty(backend: Cgroup2Backend):
    assert backend.confirm_membership(backend.create("tool_1_2")) is False


def test_stats_parse_peak_pressure_and_events(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    (handle.path / "memory.peak").write_text("1073741824\n", encoding="utf-8")
    (handle.path / "memory.current").write_text("104857600\n", encoding="utf-8")
    (handle.path / "memory.pressure").write_text(
        "some avg10=1.00 avg60=0.00 avg300=0.00 total=900000\n"
        "full avg10=1.00 avg60=0.00 avg300=0.00 total=340000\n",
        encoding="utf-8",
    )
    (handle.path / "memory.events").write_text("low 0\nhigh 239\nmax 4\noom 1\noom_kill 2\n", encoding="utf-8")

    stats = backend.read_stats(handle)
    assert stats.peak_memory_mb == pytest.approx(1024.0)
    assert stats.memory_stall_s == pytest.approx(0.34)
    assert stats.high_events == 239
    assert stats.max_events == 4
    assert stats.oom_kills == 2
    assert stats.observable is True


def test_peak_falls_back_to_running_max_of_current(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    (handle.path / "memory.current").write_text("209715200", encoding="utf-8")
    backend.read_stats(handle)
    (handle.path / "memory.current").write_text("104857600", encoding="utf-8")
    assert backend.read_stats(handle).peak_memory_mb == pytest.approx(200.0)


def test_missing_stat_files_degrade_to_zero_not_an_exception(backend: Cgroup2Backend):
    stats = backend.read_stats(backend.create("tool_1_2"))
    assert stats.peak_memory_mb == 0.0
    assert stats.memory_stall_s == 0.0


def test_freeze_and_thaw_flip_the_control_file(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    assert backend.freeze(handle) is True
    assert (handle.path / "cgroup.freeze").read_text(encoding="utf-8") == "1"
    assert handle.froze is True
    assert backend.thaw(handle) is True
    assert (handle.path / "cgroup.freeze").read_text(encoding="utf-8") == "0"


def test_destroy_removes_an_empty_cgroup(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    assert backend.destroy(handle) is True
    assert not handle.path.exists()


def test_destroy_kills_survivors_before_giving_up(backend: Cgroup2Backend, monkeypatch):
    monkeypatch.setattr(cgroup_module, "DESTROY_RETRIES", 1)
    handle = backend.create("tool_1_2")
    (handle.path / "cgroup.procs").write_text("4242", encoding="utf-8")
    assert backend.destroy(handle) is False
    assert (handle.path / "cgroup.kill").read_text(encoding="utf-8") == "1"


def test_null_backend_records_instead_of_enforcing():
    backend = NullBackend()
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("memory:high", env={}, total_bytes=16 * GB))
    assert handle.path is None
    assert handle.applied["cpu.weight"] == "100"
    assert backend.read_stats(handle).observable is False
    assert backend.freeze(handle) is False
    assert backend.destroy(handle) is True


def test_selection_falls_back_when_the_mount_is_not_there(tmp_path: Path):
    assert isinstance(select_backend(root=tmp_path / "absent"), NullBackend)


def test_selection_can_be_forced_null(fake_cgroupfs: Path):
    assert isinstance(select_backend(root=fake_cgroupfs, force_null=True), NullBackend)


@pytest.mark.skipif(os.name != "posix", reason="cgroup v2 selection only applies on Linux")
def test_selection_picks_the_real_backend_when_the_mount_is_shaped_right(fake_cgroupfs: Path):
    assert isinstance(select_backend(root=fake_cgroupfs), Cgroup2Backend)
