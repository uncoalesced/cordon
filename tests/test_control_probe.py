# Engineered by uncoalesced

from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from features.control import probe as probe_module
from features.control.probe import (
    CAP_BPF,
    CAP_SYS_ADMIN,
    Capability,
    describe_environment,
    enforcement_tier,
    probe,
    probe_capabilities,
    probe_cgroup2,
    probe_cgroup2_writable,
    probe_memcg_bpf_ops,
    probe_platform,
    probe_psi,
    probe_sched_ext,
    render,
)


@pytest.fixture
def cgroup_root(tmp_path: Path) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n", encoding="utf-8")
    return root


def test_cgroup2_is_detected_from_the_controller_list(cgroup_root: Path):
    result = probe_cgroup2(cgroup_root)
    assert result.available is True
    assert "memory" in result.detail


def test_cgroup2_missing_controllers_is_reported_as_unavailable(tmp_path: Path):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpuset io pids\n", encoding="utf-8")
    result = probe_cgroup2(root)
    assert result.available is False
    assert "missing=cpu memory" in result.detail


def test_an_absent_mount_is_reported_not_raised(tmp_path: Path):
    result = probe_cgroup2(tmp_path / "absent")
    assert result.available is False
    assert "no " in result.detail


def test_a_denied_mount_is_distinguished_from_a_missing_one(monkeypatch, cgroup_root: Path):
    def denied(_path: Path) -> str:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(probe_module, "_read", denied)
    result = probe_cgroup2(cgroup_root)
    assert result.available is False
    assert "access is denied" in result.detail


def test_a_denied_btf_is_distinguished_from_a_missing_one(tmp_path: Path, monkeypatch):
    btf = tmp_path / "vmlinux"
    btf.write_bytes(b"anything")

    def denied(*_a, **_k):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert "access is denied" in probe_memcg_bpf_ops(btf).detail


def test_missing_btf_says_why_it_matters(tmp_path: Path):
    assert "CO-RE" in probe_memcg_bpf_ops(tmp_path / "absent").detail


def _status(cap_eff: int, cap_bnd: int) -> str:
    return f"Name:\tsh\nCapEff:\t{cap_eff:016x}\nCapBnd:\t{cap_bnd:016x}\n"


def test_an_empty_bounding_set_is_called_out_as_permanent(tmp_path: Path):
    status = tmp_path / "status"
    status.write_text(_status(0, 0), encoding="utf-8")
    result = probe_capabilities(status)
    assert result.available is False
    assert "bounding set is empty" in result.detail


def test_capabilities_held_but_none_of_the_useful_ones(tmp_path: Path):
    status = tmp_path / "status"
    status.write_text(_status(1 << 3, 1 << 3), encoding="utf-8")
    result = probe_capabilities(status)
    assert result.available is False
    assert "holds none of" in result.detail


@pytest.mark.parametrize("bit,name", [(CAP_SYS_ADMIN, "CAP_SYS_ADMIN"), (CAP_BPF, "CAP_BPF")])
def test_the_enforcement_capabilities_are_recognised(tmp_path: Path, bit: int, name: str):
    status = tmp_path / "status"
    status.write_text(_status(1 << bit, (1 << 64) - 1), encoding="utf-8")
    result = probe_capabilities(status)
    assert result.available is True
    assert name in result.detail


def test_unreadable_status_degrades(tmp_path: Path):
    assert probe_capabilities(tmp_path / "absent").available is False


def test_a_termux_environment_names_itself():
    described = describe_environment(
        {"TERMUX__ROOTFS_DIR": "/data/data/com.termux/files", "TERMUX_APP_PID": "3325"}
    )
    assert "Termux" in described


def test_a_proot_environment_names_itself():
    assert "proot" in describe_environment({"PROOT_TMP_DIR": "/tmp"})


def test_an_ordinary_environment_names_nothing():
    assert describe_environment({}) in ("", "Android")


def test_the_platform_line_carries_the_environment():
    detail = probe_platform({"TERMUX_APP_PID": "1", "TERMUX__ROOTFS_DIR": "/x/com.termux/y"}).detail
    assert "Termux" in detail


def _fake_uname(monkeypatch):
    monkeypatch.setattr(
        probe_module.os,
        "uname",
        lambda: SimpleNamespace(release="4.14.356-x", sysname="Linux"),
        raising=False,
    )


def test_kernel_release_prefers_the_uname_syscall_over_platform(monkeypatch):
    monkeypatch.setattr(probe_module.platform, "uname", lambda: SimpleNamespace(release="16", version=""))
    _fake_uname(monkeypatch)
    assert probe_module.kernel_release() == "4.14.356-x"
    assert probe_module.kernel_sysname() == "Linux"


def test_an_android_kernel_is_still_reported_as_linux(monkeypatch):
    _fake_uname(monkeypatch)
    monkeypatch.setattr(probe_module.platform, "system", lambda: "Android")
    monkeypatch.setattr(probe_module.os, "name", "posix")
    result = probe_platform({"TERMUX_APP_PID": "1", "TERMUX__ROOTFS_DIR": "/x/com.termux/y"})
    assert result.available is True
    assert "Linux 4.14.356-x" in result.detail
    assert "Android" in result.detail and "Termux" in result.detail


def test_a_4_14_kernel_is_correctly_called_too_old(tmp_path: Path):
    result = probe_sched_ext(tmp_path / "absent", release="4.14.356-openela-rc1-PoWeR")
    assert result.available is False
    assert "below 6.12" in result.detail


def test_writability_is_probed_by_actually_creating_a_child(cgroup_root: Path):
    result = probe_cgroup2_writable(cgroup_root)
    assert result.available is True
    assert not (cgroup_root / "cordon_probe").exists()


def test_unwritable_root_is_reported(tmp_path: Path):
    result = probe_cgroup2_writable(tmp_path / "absent" / "deeper" / "still")
    assert result.available is False
    assert "cannot create" in result.detail


def test_sched_ext_reports_the_kernel_version_when_too_old(tmp_path: Path):
    result = probe_sched_ext(tmp_path / "absent", release="6.6.114.1-microsoft-standard-WSL2")
    assert result.available is False
    assert "below 6.12" in result.detail


def test_sched_ext_distinguishes_new_kernel_without_the_interface(tmp_path: Path):
    result = probe_sched_ext(tmp_path / "absent", release="6.15.11-generic")
    assert result.available is False
    assert "new enough" in result.detail


def test_sched_ext_present(tmp_path: Path):
    directory = tmp_path / "sched_ext"
    directory.mkdir()
    assert probe_sched_ext(directory, release="6.15.11-generic").available is True


@pytest.mark.parametrize("release", ["not-a-version", "10", ""])
def test_a_release_with_no_kernel_version_is_not_claimed_to_be_too_old(tmp_path: Path, release: str):
    result = probe_sched_ext(tmp_path / "absent", release=release)
    assert result.available is False
    assert "below" not in result.detail


def test_memcg_hooks_are_looked_for_in_btf(tmp_path: Path):
    btf = tmp_path / "vmlinux"
    btf.write_bytes(b"\x00\x01mem_cgroup\x00some_other_struct\x00")
    assert probe_memcg_bpf_ops(btf).available is False

    btf.write_bytes(b"\x00\x01memcg_bpf_ops\x00")
    assert probe_memcg_bpf_ops(btf).available is True


def test_missing_btf_is_reported_not_raised(tmp_path: Path):
    assert probe_memcg_bpf_ops(tmp_path / "absent").available is False


def test_psi_detection(tmp_path: Path):
    pressure = tmp_path / "memory"
    pressure.write_text("some avg10=0.00 total=0\nfull avg10=0.00 total=0\n", encoding="utf-8")
    assert probe_psi(pressure).available is True
    assert probe_psi(tmp_path / "absent").available is False


@pytest.mark.parametrize(
    "flags,expected",
    [
        ({"cgroup2": False, "cgroup2_writable": False, "sched_ext": False, "memcg_bpf_ops": False, "capabilities": False}, "none"),
        ({"cgroup2": True, "cgroup2_writable": True, "sched_ext": False, "memcg_bpf_ops": False, "capabilities": True}, "cgroup2"),
        ({"cgroup2": True, "cgroup2_writable": True, "sched_ext": True, "memcg_bpf_ops": False, "capabilities": True}, "cgroup2"),
        ({"cgroup2": True, "cgroup2_writable": True, "sched_ext": True, "memcg_bpf_ops": True, "capabilities": True}, "bpf"),
        ({"cgroup2": True, "cgroup2_writable": True, "sched_ext": True, "memcg_bpf_ops": True, "capabilities": False}, "cgroup2"),
        ({"cgroup2": True, "cgroup2_writable": False, "sched_ext": True, "memcg_bpf_ops": True, "capabilities": True}, "none"),
    ],
)
def test_enforcement_tier_needs_cgroups_before_anything_else(flags: dict, expected: str):
    caps = [Capability(name, value, "") for name, value in flags.items()]
    assert enforcement_tier(caps) == expected


def test_a_raising_probe_degrades_to_unavailable(monkeypatch):
    def boom() -> Capability:
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(probe_module, "probe_psi", boom)
    results = probe(Path("/nonexistent-cordon-root"))
    assert any(not cap.available for cap in results)


def test_probe_runs_on_this_machine_and_renders():
    capabilities = probe()
    names = {cap.name for cap in capabilities}
    assert {"linux", "capabilities", "cgroup2", "sched_ext", "memcg_bpf_ops"} <= names

    rendered = render(capabilities)
    assert "enforcement tier:" in rendered
    for cap in capabilities:
        assert cap.name in rendered
