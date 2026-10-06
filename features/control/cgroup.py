# Engineered by uncoalesced

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from features import host
from features.control.intent import Intent
from features.control.probe import (
    CGROUP2_ROOT,
    PROC_SELF_CGROUP,
    REQUIRED_CONTROLLERS,
    find_delegated_base,
    own_cgroup,
    systemd_run_user_works,
)
from features.wrapper.logging_setup import get_logger, log_failure

PARENT_NAME = "cordon"
DESTROY_RETRIES = 20
DESTROY_PAUSE_S = 0.05
PROC_ROOT = Path("/proc")

SKIPPED = "skipped: controller not delegated"

_BYTES_PER_MB = 1024.0 * 1024.0


@dataclass
class CgroupHandle:
    name: str
    backend: str
    path: Path | None = None
    observed_peak_bytes: int = 0
    froze: bool = False
    applied: dict[str, str] = field(default_factory=dict)
    pid: int = 0
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = str(self.path) if self.path else ""
        return payload


@dataclass
class CgroupStats:
    peak_memory_mb: float = 0.0
    memory_stall_s: float = 0.0
    cpu_stall_s: float = 0.0
    high_events: int = 0
    max_events: int = 0
    oom_kills: int = 0
    froze: bool = False
    observable: bool = False
    # "psi" for kernel pressure stall time, "watchdog" for advisory over-limit seconds, "" when
    # nothing was measured. Analysis must never sum the two as if they were the same quantity.
    stall_source: str = ""
    attach_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def call_cgroup_name(pid: int | None = None, ts_ns: int | None = None) -> str:
    pid = os.getpid() if pid is None else pid
    ts_ns = time.time_ns() if ts_ns is None else ts_ns
    return f"tool_{pid}_{ts_ns}"


def attach_in_child(backend: Any, handle: CgroupHandle, fd: int | None) -> None:
    """preexec_fn body: join the cgroup, and report failure as b"E<errno>" on an inherited pipe.

    Runs between fork and exec, so no logging, no locks: one syscall-shaped write and out.
    """
    try:
        backend.join_self(handle)
    except BaseException as exc:  # noqa: BLE001 - an exception escaping preexec_fn aborts the spawn
        if fd is not None:
            try:
                os.write(fd, b"E%d" % (getattr(exc, "errno", None) or 0))
            except OSError:
                pass


class NullBackend:
    name = "null"

    def __init__(self) -> None:
        self.log = get_logger("cgroup.null")

    def available(self) -> bool:
        return True

    def create(self, name: str) -> CgroupHandle:
        self.log.info("no cgroup backend; recording intent only | name=%s", name)
        return CgroupHandle(name=name, backend=self.name)

    def apply(self, handle: CgroupHandle, intent: Intent) -> None:
        handle.applied = {
            "memory.high": intent.memory_high_value,
            "cpu.weight": str(intent.cpu_weight),
        }
        self.log.info("would apply | name=%s %s", handle.name, handle.applied)

    def wrap_argv(self, handle: CgroupHandle, argv: list[str]) -> list[str]:
        return argv

    def join_self(self, handle: CgroupHandle) -> None:
        return None

    def bind_pid(self, handle: CgroupHandle, pid: int) -> None:
        handle.pid = pid

    def confirm_membership(self, handle: CgroupHandle) -> bool:
        return False

    def read_stats(self, handle: CgroupHandle) -> CgroupStats:
        return CgroupStats(froze=handle.froze, observable=False)

    def freeze(self, handle: CgroupHandle) -> bool:
        return False

    def thaw(self, handle: CgroupHandle) -> bool:
        return False

    def destroy(self, handle: CgroupHandle) -> bool:
        return True


class Cgroup2Backend(NullBackend):
    """Per-call cgroups under <base>/cordon/. base is the mount root, or a delegated ancestor."""

    name = "cgroup2"

    def __init__(
        self,
        root: Path = CGROUP2_ROOT,
        parent_name: str = PARENT_NAME,
        base: Path | None = None,
        mode: str = "root",
    ) -> None:
        self.root = Path(root)
        self.base = self.root if base is None else Path(base)
        self.parent = self.base / parent_name
        self.mode = mode
        self.controllers: tuple[str, ...] | None = None
        self.log = get_logger("cgroup.v2")

    def available(self) -> bool:
        try:
            controllers = (self.root / "cgroup.controllers").read_text(encoding="utf-8").split()
        except OSError:
            return False
        return all(c in controllers for c in REQUIRED_CONTROLLERS)

    def _write(self, path: Path, value: str, critical: bool = False) -> bool:
        try:
            path.write_text(value, encoding="utf-8")
            return True
        except OSError:
            if critical:
                log_failure(self.log, "cgroup write failed", path=str(path), value=value)
            else:
                self.log.warning("cgroup write failed, continuing | path=%s value=%s", path, value)
            return False

    def _read(self, path: Path, default: str = "") -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return default

    def _enabled(self, directory: Path) -> set[str]:
        return {c.lstrip("+") for c in self._read(directory / "cgroup.subtree_control").split()}

    def _delegate(self, directory: Path) -> None:
        # The kernel rejects the whole write if any token is unavailable, and a delegated user
        # subtree often has memory but not cpu, so fall back to one controller per write.
        enabled = self._enabled(directory)
        missing = [c for c in REQUIRED_CONTROLLERS if c not in enabled]
        target = directory / "cgroup.subtree_control"
        if not missing or self._write(target, " ".join(f"+{c}" for c in missing)):
            return
        for controller in missing:
            self._write(target, f"+{controller}")

    def setup(self) -> tuple[str, ...]:
        """Build base/cordon once and return the controllers its children will actually get."""
        if self.controllers is not None:
            return self.controllers
        # No internal processes: a populated non-root cgroup cannot enable controllers, so leave it
        # alone and rely on what is already enabled there. The mount root is exempt from the rule.
        if self.base == self.root or not self._read(self.base / "cgroup.procs"):
            self._delegate(self.base)
        self.parent.mkdir(parents=True, exist_ok=True)
        self._delegate(self.parent)
        self.controllers = tuple(c for c in REQUIRED_CONTROLLERS if c in self._enabled(self.parent))
        self.log.info("cgroup parent ready | mode=%s path=%s controllers=%s", self.mode, self.parent, self.controllers)
        return self.controllers

    def usable(self) -> bool:
        """The real test: set up the parent, then create and remove a child."""
        try:
            if not self.setup():
                return False
            probe = self.parent / f"probe_{os.getpid()}_{time.time_ns()}"
            probe.mkdir()
            probe.rmdir()
            return True
        except OSError as exc:
            self.log.info("cgroup2 %s backend not usable at %s: %s", self.mode, self.base, exc)
            return False

    def create(self, name: str) -> CgroupHandle:
        self.setup()
        path = self.parent / name
        try:
            path.mkdir(exist_ok=False)
        except FileExistsError:
            # Two calls in the same nanosecond from the same pid (or a leaked cgroup): retry once.
            name = f"{name}_{time.time_ns()}"
            path = self.parent / name
            path.mkdir(exist_ok=False)
        self.log.info("cgroup created | path=%s", path)
        return CgroupHandle(name=name, backend=self.name, path=path)

    def apply(self, handle: CgroupHandle, intent: Intent) -> None:
        if handle.path is None:
            return
        controllers = self.controllers if self.controllers is not None else REQUIRED_CONTROLLERS
        applied: dict[str, str] = {}
        for filename, value, controller in (
            ("memory.high", intent.memory_high_value, "memory"),
            ("cpu.weight", str(intent.cpu_weight), "cpu"),
            ("memory.oom.group", "1", "memory"),
        ):
            if controller not in controllers:
                applied[filename] = SKIPPED
            elif self._write(handle.path / filename, value):
                applied[filename] = value
        handle.applied = applied
        self.log.info("cgroup limits applied | path=%s %s", handle.path, applied)

    def join_self(self, handle: CgroupHandle) -> None:
        # Called in the child between fork and exec: raw os calls only, and errors propagate so
        # attach_in_child can report the errno to the parent.
        if handle.path is None:
            return
        fd = os.open(str(handle.path / "cgroup.procs"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)

    def confirm_membership(self, handle: CgroupHandle) -> bool:
        if handle.path is None:
            return False
        return bool(self._read(handle.path / "cgroup.procs"))

    def _pressure_total_s(self, path: Path) -> float:
        for line in self._read(path).splitlines():
            if not line.startswith("full"):
                continue
            for field_text in line.split():
                if field_text.startswith("total="):
                    try:
                        return int(field_text.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        return 0.0
        return 0.0

    def _events(self, path: Path) -> dict[str, int]:
        parsed: dict[str, int] = {}
        for line in self._read(path).splitlines():
            key, _, value = line.partition(" ")
            try:
                parsed[key] = int(value)
            except ValueError:
                continue
        return parsed

    def read_stats(self, handle: CgroupHandle) -> CgroupStats:
        if handle.path is None:
            return CgroupStats(observable=False)

        peak = self._read(handle.path / "memory.peak")
        current = self._read(handle.path / "memory.current", "0")
        for candidate in (peak, current):
            try:
                handle.observed_peak_bytes = max(handle.observed_peak_bytes, int(candidate))
            except ValueError:
                continue

        events = self._events(handle.path / "memory.events")
        return CgroupStats(
            peak_memory_mb=round(handle.observed_peak_bytes / _BYTES_PER_MB, 3),
            memory_stall_s=self._pressure_total_s(handle.path / "memory.pressure"),
            cpu_stall_s=self._pressure_total_s(handle.path / "cpu.pressure"),
            high_events=events.get("high", 0),
            max_events=events.get("max", 0),
            oom_kills=events.get("oom_kill", 0),
            froze=handle.froze,
            observable=True,
            stall_source="psi",
        )

    def freeze(self, handle: CgroupHandle) -> bool:
        if handle.path is None or not self._write(handle.path / "cgroup.freeze", "1"):
            return False
        handle.froze = True
        self.log.info("cgroup frozen | path=%s", handle.path)
        return True

    def thaw(self, handle: CgroupHandle) -> bool:
        if handle.path is None:
            return False
        return self._write(handle.path / "cgroup.freeze", "0")

    def destroy(self, handle: CgroupHandle) -> bool:
        if handle.path is None:
            return True
        path = handle.path
        if handle.froze:
            self.thaw(handle)
        if self._read(path / "cgroup.procs"):
            self.log.warning("cgroup still populated at teardown, killing | path=%s", path)
            self._write(path / "cgroup.kill", "1")

        for _ in range(DESTROY_RETRIES):
            try:
                path.rmdir()
                return True
            except FileNotFoundError:
                return True
            except OSError:
                time.sleep(DESTROY_PAUSE_S)

        log_failure(self.log, "could not remove cgroup, leaking it", path=str(path))
        return False


class SystemdRunBackend(Cgroup2Backend):
    """Let the user's systemd create the cgroup: `systemd-run --user --scope` around the command.

    The scope is collected by systemd when its last process exits, so there is nothing to tear
    down. Stats are read from the scope's own cgroup, located from /proc/<pid>/cgroup once
    systemd-run has moved itself in and exec'd the command (same pid).
    """

    name = "systemd-run"

    def __init__(
        self,
        root: Path = CGROUP2_ROOT,
        binary: str = "systemd-run",
        proc_root: Path = PROC_ROOT,
    ) -> None:
        super().__init__(root=root, mode="systemd")
        self.binary = binary
        self.proc_root = Path(proc_root)
        self.controllers = REQUIRED_CONTROLLERS
        self._last: dict[str, CgroupStats] = {}
        self.log = get_logger("cgroup.systemd")

    def usable(self, runner: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] | None = None) -> bool:
        kwargs: dict[str, Any] = {"runner": runner}
        if which is not None:
            kwargs["which"] = which
        ok, detail = systemd_run_user_works(self.binary, **kwargs)
        if not ok:
            self.log.info("systemd-run backend not usable: %s", detail)
        return ok

    def create(self, name: str) -> CgroupHandle:
        return CgroupHandle(name=name, backend=self.name, unit=f"cordon-{name}")

    def apply(self, handle: CgroupHandle, intent: Intent) -> None:
        high = "infinity" if intent.memory_high_bytes is None else str(intent.memory_high_bytes)
        handle.applied = {"MemoryHigh": high, "CPUWeight": str(intent.cpu_weight)}

    def wrap_argv(self, handle: CgroupHandle, argv: list[str]) -> list[str]:
        props: list[str] = []
        for key in ("MemoryHigh", "CPUWeight"):
            if key in handle.applied:
                props += ["-p", f"{key}={handle.applied[key]}"]
        return [
            self.binary, "--user", "--scope", "--quiet", "--collect",
            f"--unit={handle.unit}", *props, "--", *argv,
        ]  # fmt: skip

    def join_self(self, handle: CgroupHandle) -> None:
        return None

    def bind_pid(self, handle: CgroupHandle, pid: int) -> None:
        handle.pid = pid
        self._locate(handle)

    def _locate(self, handle: CgroupHandle) -> None:
        if handle.path is not None or not handle.pid:
            return
        rel = own_cgroup(self.proc_root / str(handle.pid) / "cgroup")
        # Until systemd-run has registered the scope the pid still sits in our own cgroup.
        if rel and rel.rstrip("/").endswith(f"/{handle.unit}.scope"):
            handle.path = self.root / rel.strip("/")

    def confirm_membership(self, handle: CgroupHandle) -> bool:
        self._locate(handle)
        return handle.path is not None

    def read_stats(self, handle: CgroupHandle) -> CgroupStats:
        self._locate(handle)
        if handle.path is None or not handle.path.is_dir():
            # --collect removes the scope the moment the command exits; keep the last live view.
            return self._last.get(handle.name, CgroupStats(observable=False))
        stats = super().read_stats(handle)
        self._last[handle.name] = stats
        return stats

    def destroy(self, handle: CgroupHandle) -> bool:
        self._last.pop(handle.name, None)
        return True


def select_backend(
    root: Path = CGROUP2_ROOT,
    force_null: bool = False,
    os_kind: str | None = None,
    proc_self_cgroup: Path = PROC_SELF_CGROUP,
    systemd: SystemdRunBackend | None = None,
) -> Any:
    """First backend that passes a real probe: delegated cgroup, root cgroup, systemd scope,
    advisory watchdog, null. Each candidate actually creates and removes something."""
    from features.control.advisory import AdvisoryBackend

    log = get_logger("cgroup")
    os_kind = host.OS if os_kind is None else os_kind
    if force_null or os_kind not in (host.LINUX, host.DARWIN):
        return NullBackend()
    if os_kind == host.DARWIN:
        return AdvisoryBackend(os_kind=os_kind)

    # uid 0 can write every node, so "nearest writable ancestor" would land inside system.slice;
    # root gets the mount root instead, everyone else tries their delegated subtree first.
    is_root = getattr(os, "geteuid", lambda: -1)() == 0
    delegated = None if is_root else find_delegated_base(root, proc_self_cgroup)
    if delegated is not None:
        backend = Cgroup2Backend(root=root, base=delegated[0], mode="delegated")
        if backend.usable():
            return backend
    backend = Cgroup2Backend(root=root)
    if backend.available() and backend.usable():
        return backend
    systemd = SystemdRunBackend(root=root) if systemd is None else systemd
    if systemd.usable():
        return systemd
    log.warning("no usable cgroup v2 at %s, falling back to the advisory watchdog", root)
    return AdvisoryBackend(os_kind=os_kind)
