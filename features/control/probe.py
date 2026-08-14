# Engineered by uncoalesced

from __future__ import annotations

import errno
import os
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from features.wrapper.logging_setup import get_logger, log_failure

CGROUP2_ROOT = Path("/sys/fs/cgroup")
SCHED_EXT_DIR = Path("/sys/kernel/sched_ext")
BTF_VMLINUX = Path("/sys/kernel/btf/vmlinux")
PSI_MEMORY = Path("/proc/pressure/memory")
PROC_STATUS = Path("/proc/self/status")
ANDROID_MARKER = Path("/system/build.prop")

MEMCG_STRUCT_OPS = b"memcg_bpf_ops"
SCHED_EXT_MIN_KERNEL = (6, 12)

REQUIRED_CONTROLLERS = ("cpu", "memory")

CAP_SYS_ADMIN = 21
CAP_SYS_RESOURCE = 24
CAP_PERFMON = 38
CAP_BPF = 39

ENFORCEMENT_CAPS = {
    "CAP_SYS_ADMIN": CAP_SYS_ADMIN,
    "CAP_SYS_RESOURCE": CAP_SYS_RESOURCE,
    "CAP_PERFMON": CAP_PERFMON,
    "CAP_BPF": CAP_BPF,
}


@dataclass
class Capability:
    name: str
    available: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def kernel_release() -> str:
    # platform.uname() reports the Android release ("16") on Android builds of CPython,
    # not the kernel. os.uname() is the syscall and reports the kernel on every POSIX host.
    try:
        return os.uname().release
    except AttributeError:
        pass
    except Exception:
        return ""
    try:
        return platform.uname().release
    except Exception:
        return ""


def kernel_sysname() -> str:
    try:
        return os.uname().sysname
    except AttributeError:
        pass
    except Exception:
        return ""
    try:
        return platform.system()
    except Exception:
        return ""


def _kernel_version(release: str) -> tuple[int, int]:
    parts = release.split(".")
    try:
        return int(parts[0]), int(parts[1].split("-")[0])
    except (IndexError, ValueError):
        return (0, 0)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _denied(exc: OSError) -> bool:
    return exc.errno in (errno.EACCES, errno.EPERM)


def describe_environment(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    labels: list[str] = []
    android = (
        sys.platform == "android"
        or platform.system() == "Android"
        or bool(env.get("ANDROID_ROOT"))
        or ANDROID_MARKER.exists()
    )
    if android:
        labels.append("Android")
    rootfs = env.get("TERMUX__ROOTFS_DIR") or env.get("PREFIX", "")
    if "com.termux" in rootfs or env.get("TERMUX_VERSION") or env.get("TERMUX_APP_PID"):
        labels.append("Termux app sandbox")
    if env.get("PROOT_L2S_DIR") or env.get("PROOT_TMP_DIR"):
        labels.append("proot")
    return ", ".join(labels)


def probe_platform(env: dict[str, str] | None = None) -> Capability:
    release = kernel_release()
    sysname = kernel_sysname()
    linux = os.name == "posix" and sysname == "Linux"
    detail = f"{sysname} {release}".strip()
    environment = describe_environment(env)
    if environment:
        detail = f"{detail} ({environment})"
    return Capability(name="linux", available=linux, detail=detail)


def probe_capabilities(status_path: Path = PROC_STATUS) -> Capability:
    try:
        status = _read(Path(status_path))
    except OSError:
        return Capability("capabilities", False, f"no {status_path} to read capability bits from")

    bits: dict[str, int] = {}
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapBnd"):
            try:
                bits[key] = int(value.strip(), 16)
            except ValueError:
                continue

    effective = bits.get("CapEff", 0)
    bounding = bits.get("CapBnd", 0)
    held = [name for name, bit in ENFORCEMENT_CAPS.items() if effective & (1 << bit)]

    if bounding == 0:
        return Capability(
            "capabilities",
            False,
            "bounding set is empty: no capability can ever be acquired by this process",
        )
    if not held:
        return Capability("capabilities", False, f"CapEff={effective:016x} holds none of {sorted(ENFORCEMENT_CAPS)}")
    return Capability("capabilities", True, f"holds {' '.join(sorted(held))}")


def probe_cgroup2(root: Path = CGROUP2_ROOT) -> Capability:
    log = get_logger("probe")
    controllers_file = Path(root) / "cgroup.controllers"
    try:
        controllers = _read(controllers_file).split()
    except OSError as exc:
        if _denied(exc):
            return Capability("cgroup2", False, f"{controllers_file} exists but access is denied")
        return Capability("cgroup2", False, f"no {controllers_file}")

    missing = [c for c in REQUIRED_CONTROLLERS if c not in controllers]
    if missing:
        log.warning("cgroup2 present but missing controllers | missing=%s", missing)
        return Capability("cgroup2", False, f"controllers={' '.join(controllers)} missing={' '.join(missing)}")
    return Capability("cgroup2", True, f"controllers={' '.join(controllers)}")


def probe_cgroup2_writable(root: Path = CGROUP2_ROOT) -> Capability:
    log = get_logger("probe")
    candidate = Path(root) / "cordon_probe"
    try:
        candidate.mkdir(exist_ok=True)
    except OSError as exc:
        reason = "no delegated write access" if _denied(exc) else (exc.strerror or str(exc))
        return Capability("cgroup2_writable", False, f"cannot create {candidate}: {reason}")
    try:
        candidate.rmdir()
    except OSError:
        log_failure(log, "probe cgroup left behind", path=str(candidate))
    return Capability("cgroup2_writable", True, f"can create and remove children under {root}")


def probe_psi(path: Path = PSI_MEMORY) -> Capability:
    try:
        _read(Path(path))
    except OSError:
        return Capability("psi", False, f"no {path}; throttle stall time will read as zero")
    return Capability("psi", True, "memory pressure accounting available")


def probe_sched_ext(directory: Path = SCHED_EXT_DIR, release: str | None = None) -> Capability:
    release = kernel_release() if release is None else release
    major, minor = _kernel_version(release)
    if Path(directory).is_dir():
        return Capability("sched_ext", True, f"{directory} present on {release}")
    if (major, minor) == (0, 0):
        return Capability("sched_ext", False, f"no Linux kernel version to compare ({release!r})")
    if (major, minor) < SCHED_EXT_MIN_KERNEL:
        return Capability(
            "sched_ext",
            False,
            f"kernel {release} is below {SCHED_EXT_MIN_KERNEL[0]}.{SCHED_EXT_MIN_KERNEL[1]}",
        )
    return Capability("sched_ext", False, f"kernel {release} is new enough but {directory} is absent")


def probe_memcg_bpf_ops(btf: Path = BTF_VMLINUX) -> Capability:
    log = get_logger("probe")
    btf = Path(btf)
    try:
        blob = btf.read_bytes()
    except OSError as exc:
        if _denied(exc):
            return Capability("memcg_bpf_ops", False, f"{btf} exists but access is denied")
        return Capability("memcg_bpf_ops", False, f"no {btf} to inspect; CO-RE needs kernel BTF")
    except MemoryError:
        log_failure(log, "BTF blob too large to inspect", path=str(btf))
        return Capability("memcg_bpf_ops", False, "BTF unreadable")

    if MEMCG_STRUCT_OPS in blob:
        return Capability("memcg_bpf_ops", True, f"{MEMCG_STRUCT_OPS.decode()} struct_ops found in BTF")
    return Capability("memcg_bpf_ops", False, "struct_ops absent; RFC patch series is not applied")


def probe(root: Path = CGROUP2_ROOT) -> list[Capability]:
    log = get_logger("probe")
    checks = (
        probe_platform,
        probe_capabilities,
        lambda: probe_cgroup2(root),
        lambda: probe_cgroup2_writable(root),
        probe_psi,
        probe_sched_ext,
        probe_memcg_bpf_ops,
    )
    results: list[Capability] = []
    for check in checks:
        try:
            results.append(check())
        except Exception:
            log_failure(log, "capability probe raised, reporting unavailable", check=getattr(check, "__name__", "lambda"))
            results.append(Capability(getattr(check, "__name__", "unknown"), False, "probe raised"))
    return results


def enforcement_tier(capabilities: list[Capability]) -> str:
    by_name = {cap.name: cap.available for cap in capabilities}
    if not by_name.get("cgroup2") or not by_name.get("cgroup2_writable"):
        return "none"
    if by_name.get("sched_ext") and by_name.get("memcg_bpf_ops") and by_name.get("capabilities"):
        return "bpf"
    return "cgroup2"


def render(capabilities: list[Capability]) -> str:
    tier = enforcement_tier(capabilities)
    width = max(len(cap.name) for cap in capabilities)
    lines = [f"enforcement tier: {tier}", ""]
    for cap in capabilities:
        mark = "yes" if cap.available else "no "
        lines.append(f"  {cap.name.ljust(width)}  {mark}  {cap.detail}")
    lines.append("")
    lines.append(_TIER_NOTES[tier])
    return "\n".join(lines)


_TIER_NOTES = {
    "none": (
        "No cgroup v2 available. Cordon will record what it would have enforced and run the\n"
        "command unchanged. See docs/stage2-design.md for what unblocks real enforcement."
    ),
    "cgroup2": (
        "cgroup v2 enforcement available: per-call memory.high, cpu.weight, freeze and kill,\n"
        "with PSI stall accounting. The in-kernel BPF policy layer (sched_ext for CPU,\n"
        "memcg_bpf_ops for memory) is absent, so throttle decisions stay at kernel default."
    ),
    "bpf": (
        "Full enforcement available: cgroup v2 plus the in-kernel BPF policy layer."
    ),
}
