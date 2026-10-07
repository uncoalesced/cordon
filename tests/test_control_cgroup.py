# Engineered by uncoalesced

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from features import host
from features.control import cgroup as cgroup_module
from features.control.advisory import AdvisoryBackend
from features.control.cgroup import (
    SKIPPED,
    Cgroup2Backend,
    CgroupHandle,
    NullBackend,
    SystemdRunBackend,
    attach_in_child,
    call_cgroup_name,
    select_backend,
)
from features.control.intent import MEMORY_TIER_FLOOR_MB, resolve_intent
from features.control.probe import find_delegated_base

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
    assert call_cgroup_name(pid=48213, ts_ns=1755000000123456789) == "tool_48213_1755000000123456789"


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


def test_a_name_collision_retries_once_under_a_fresh_name(backend: Cgroup2Backend):
    first = backend.create("tool_1_2")
    second = backend.create("tool_1_2")
    assert first.path != second.path
    assert second.name.startswith("tool_1_2_") and second.path.is_dir()


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


class _NoSystemd(SystemdRunBackend):
    def usable(self, *a, **k) -> bool:
        return False


class _YesSystemd(SystemdRunBackend):
    def usable(self, *a, **k) -> bool:
        return True


def test_selection_can_be_forced_null(fake_cgroupfs: Path):
    assert isinstance(select_backend(root=fake_cgroupfs, force_null=True, os_kind=host.LINUX), NullBackend)


def test_windows_selects_null():
    assert isinstance(select_backend(os_kind=host.WINDOWS), NullBackend)


def test_darwin_selects_advisory():
    assert isinstance(select_backend(os_kind=host.DARWIN), AdvisoryBackend)


def test_linux_without_cgroups_or_systemd_falls_back_to_advisory(tmp_path: Path):
    chosen = select_backend(
        root=tmp_path / "absent", os_kind=host.LINUX, proc_self_cgroup=tmp_path / "nope", systemd=_NoSystemd()
    )
    assert isinstance(chosen, AdvisoryBackend)


def test_linux_without_cgroups_takes_systemd_run_when_it_works(tmp_path: Path):
    chosen = select_backend(
        root=tmp_path / "absent", os_kind=host.LINUX, proc_self_cgroup=tmp_path / "nope", systemd=_YesSystemd()
    )
    assert chosen.name == "systemd-run"


def test_linux_root_mount_is_chosen_when_it_passes_the_write_probe(fake_cgroupfs: Path, tmp_path: Path):
    chosen = select_backend(
        root=fake_cgroupfs, os_kind=host.LINUX, proc_self_cgroup=tmp_path / "nope", systemd=_NoSystemd()
    )
    assert isinstance(chosen, Cgroup2Backend) and chosen.mode == "root"
    assert chosen.controllers == ("cpu", "memory")
    assert not [p for p in chosen.parent.iterdir() if p.name.startswith("probe_")]


# --- delegated subtree --------------------------------------------------------------------------


def _node(path: Path, controllers: str, subtree: str = "", procs: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "cgroup.controllers").write_text(controllers, encoding="utf-8")
    (path / "cgroup.subtree_control").write_text(subtree, encoding="utf-8")
    (path / "cgroup.procs").write_text(procs, encoding="utf-8")
    return path


def _user_tree(tmp_path: Path, service_controllers: str = "cpu memory pids") -> tuple[Path, Path, Path]:
    """A systemd user session: root-owned slices above user@1000.service, our shell in a scope."""
    mount = tmp_path / "cgroup"
    _node(mount, "cpuset cpu io memory pids", "cpu memory pids", "1")
    _node(mount / "user.slice", "cpu memory pids", "cpu memory pids")
    _node(mount / "user.slice/user-1000.slice", "cpu memory pids", "cpu memory pids")
    service = _node(mount / "user.slice/user-1000.slice/user@1000.service", service_controllers, service_controllers)
    _node(service / "init.scope", "", "", "900")
    app = _node(service / "app.slice", service_controllers, "")
    scope = _node(app / "vte-spawn-1.scope", service_controllers, "", "4242\n4243")
    proc_file = tmp_path / "proc_self_cgroup"
    proc_file.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-1.scope\n", encoding="utf-8"
    )
    return mount, scope, proc_file


def _only_under(prefix: Path):
    def access(path, mode) -> bool:
        return str(Path(path)).startswith(str(prefix))

    return access


def test_delegated_base_skips_the_populated_scope_and_takes_the_nearest_empty_ancestor(tmp_path: Path):
    mount, scope, proc_file = _user_tree(tmp_path)
    service = scope.parent.parent
    found = find_delegated_base(mount, proc_file, access=_only_under(service))
    assert found == (scope.parent, ("cpu", "memory"))


def test_delegated_base_never_climbs_past_the_ownership_boundary(tmp_path: Path):
    mount, scope, proc_file = _user_tree(tmp_path)
    assert find_delegated_base(mount, proc_file, access=lambda p, m: False) is None


def test_delegated_base_with_internal_processes_uses_only_already_enabled_controllers(tmp_path: Path):
    mount = tmp_path / "cgroup"
    _node(mount, "cpu memory", "cpu memory")
    busy = _node(mount / "busy.service", "cpu memory", "memory", "77")
    proc_file = tmp_path / "pc"
    proc_file.write_text("0::/busy.service\n", encoding="utf-8")
    assert find_delegated_base(mount, proc_file, access=lambda p, m: True) == (busy, ("memory",))


def test_delegated_base_missing_cpu_degrades_and_records_the_skip(tmp_path: Path):
    mount, scope, proc_file = _user_tree(tmp_path, service_controllers="memory pids")
    base, controllers = find_delegated_base(mount, proc_file, access=_only_under(scope.parent.parent))
    assert controllers == ("memory",)

    backend = Cgroup2Backend(root=mount, base=base, mode="delegated")
    # The fake fs echoes writes; a real kernel would refuse +cpu here. Pretend it did.
    original = backend._write

    def kernel_like(path: Path, value: str, critical: bool = False) -> bool:
        if path.name == "cgroup.subtree_control" and "cpu" in value:
            return False
        return original(path, value, critical)

    backend._write = kernel_like
    assert backend.setup() == ("memory",)
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("memory:low,cpu:low", env={}, total_bytes=1 * GB))
    assert handle.applied["cpu.weight"] == SKIPPED
    assert handle.applied["memory.high"] == str(int(MEMORY_TIER_FLOOR_MB * 1024 * 1024))
    assert not (handle.path / "cpu.weight").exists()


def test_delegated_setup_builds_cordon_below_the_base_and_never_touches_a_populated_base(tmp_path: Path):
    mount = tmp_path / "cgroup"
    _node(mount, "cpu memory", "cpu memory")
    busy = _node(mount / "busy.service", "cpu memory", "cpu memory", "77")
    backend = Cgroup2Backend(root=mount, base=busy, mode="delegated")
    assert backend.usable() is True
    assert (busy / "cgroup.subtree_control").read_text(encoding="utf-8") == "cpu memory"
    assert (busy / "cordon" / "cgroup.subtree_control").read_text(encoding="utf-8") == "+cpu +memory"
    handle = backend.create("tool_1_2")
    assert handle.path == busy / "cordon" / "tool_1_2"


def test_select_prefers_the_delegated_subtree(tmp_path: Path):
    mount, scope, proc_file = _user_tree(tmp_path)
    chosen = select_backend(root=mount, os_kind=host.LINUX, proc_self_cgroup=proc_file, systemd=_NoSystemd())
    assert isinstance(chosen, Cgroup2Backend)
    # tmp_path is ours, so os.access says yes everywhere and the nearest empty node wins.
    assert chosen.mode == "delegated"


def test_proc_self_cgroup_without_a_unified_line_is_not_delegated(tmp_path: Path):
    proc_file = tmp_path / "pc"
    proc_file.write_text("12:memory:/user.slice\n1:name=systemd:/user.slice\n", encoding="utf-8")
    assert find_delegated_base(tmp_path, proc_file, access=lambda p, m: True) is None


# --- systemd-run --------------------------------------------------------------------------------


def test_systemd_run_wraps_argv_with_scope_properties():
    backend = SystemdRunBackend(binary="systemd-run")
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("memory:512M,cpu:low", env={}, total_bytes=16 * GB))
    assert backend.wrap_argv(handle, ["pytest", "-q"]) == [
        "systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit=cordon-tool_1_2",
        "-p", f"MemoryHigh={512 * 1024 * 1024}", "-p", "CPUWeight=25", "--", "pytest", "-q",
    ]  # fmt: skip


def test_systemd_run_unlimited_memory_is_infinity_not_max():
    backend = SystemdRunBackend()
    handle = backend.create("tool_1_2")
    backend.apply(handle, resolve_intent("memory:max", env={}, total_bytes=16 * GB))
    assert "MemoryHigh=infinity" in backend.wrap_argv(handle, ["x"])


def test_systemd_run_finds_the_scope_from_proc_pid_cgroup_and_keeps_stats_after_collection(tmp_path: Path):
    mount = tmp_path / "cgroup"
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    pid_cgroup = proc / "4242" / "cgroup"
    pid_cgroup.write_text("0::/user.slice/user-1000.slice/session-3.scope\n", encoding="utf-8")

    backend = SystemdRunBackend(root=mount, proc_root=proc)
    handle = backend.create("tool_1_2")
    backend.bind_pid(handle, 4242)
    assert backend.confirm_membership(handle) is False  # systemd-run has not moved itself yet

    rel = "user.slice/user-1000.slice/user@1000.service/app.slice/cordon-tool_1_2.scope"
    scope = mount / rel
    scope.mkdir(parents=True)
    (scope / "memory.events").write_text("low 0\nhigh 17\nmax 0\noom_kill 0\n", encoding="utf-8")
    (scope / "memory.peak").write_text(str(300 * 1024 * 1024), encoding="utf-8")
    pid_cgroup.write_text(f"0::/{rel}\n", encoding="utf-8")

    assert backend.confirm_membership(handle) is True
    assert handle.path == scope
    live = backend.read_stats(handle)
    assert live.high_events == 17 and live.stall_source == "psi"

    for child in scope.iterdir():
        child.unlink()
    scope.rmdir()  # --collect removed the scope when the command exited
    after = backend.read_stats(handle)
    assert after.high_events == 17 and after.peak_memory_mb == pytest.approx(300.0)
    assert backend.destroy(handle) is True


def test_systemd_run_usable_runs_a_real_throwaway_scope():
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr=b"")

    assert SystemdRunBackend().usable(runner=runner, which=lambda b: "/usr/bin/systemd-run") is True
    assert calls[0][:5] == ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect"]


def test_systemd_run_unusable_without_a_user_manager():
    def runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stderr=b"Failed to connect to bus: No medium found\n")

    assert SystemdRunBackend().usable(runner=runner, which=lambda b: "/usr/bin/systemd-run") is False
    assert SystemdRunBackend().usable(runner=runner, which=lambda b: None) is False


# --- attach-error pipe --------------------------------------------------------------------------


def test_join_failure_is_reported_through_the_pipe_as_an_errno(tmp_path: Path):
    backend = Cgroup2Backend(root=tmp_path)
    handle = CgroupHandle(name="gone", backend="cgroup2", path=tmp_path / "missing" / "cgroup")
    read_fd, write_fd = os.pipe()
    try:
        attach_in_child(backend, handle, write_fd)
        os.close(write_fd)
        data = os.read(read_fd, 64)
    finally:
        os.close(read_fd)
    assert data == b"E%d" % errno.ENOENT


def test_a_successful_join_writes_nothing(backend: Cgroup2Backend):
    handle = backend.create("tool_1_2")
    read_fd, write_fd = os.pipe()
    attach_in_child(backend, handle, write_fd)
    os.close(write_fd)
    assert os.read(read_fd, 64) == b""
    os.close(read_fd)


def test_a_non_oserror_in_the_child_still_reports_instead_of_aborting_the_spawn():
    class Broken(NullBackend):
        def join_self(self, handle):
            raise RuntimeError("bad")

    read_fd, write_fd = os.pipe()
    attach_in_child(Broken(), CgroupHandle(name="x", backend="b"), write_fd)
    os.close(write_fd)
    assert os.read(read_fd, 64) == b"E0"
    os.close(read_fd)
