# Engineered by uncoalesced

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from features.control.intent import Intent
from features.control.probe import CGROUP2_ROOT, REQUIRED_CONTROLLERS
from features.wrapper.logging_setup import get_logger, log_failure

PARENT_NAME = "cordon"
DESTROY_RETRIES = 20
DESTROY_PAUSE_S = 0.05

_BYTES_PER_MB = 1024.0 * 1024.0


@dataclass
class CgroupHandle:
    name: str
    backend: str
    path: Path | None = None
    observed_peak_bytes: int = 0
    froze: bool = False
    applied: dict[str, str] = field(default_factory=dict)

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def call_cgroup_name(pid: int | None = None, ts: float | None = None) -> str:
    pid = os.getpid() if pid is None else pid
    ts = time.time() if ts is None else ts
    return f"tool_{pid}_{int(ts)}"


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

    def join_self(self, handle: CgroupHandle) -> None:
        return None

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


class Cgroup2Backend:
    name = "cgroup2"

    def __init__(self, root: Path = CGROUP2_ROOT, parent_name: str = PARENT_NAME) -> None:
        self.root = Path(root)
        self.parent = self.root / parent_name
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

    def _delegate(self, directory: Path) -> None:
        enabled = self._read(directory / "cgroup.subtree_control").split()
        wanted = " ".join(f"+{c}" for c in REQUIRED_CONTROLLERS if c not in enabled)
        if wanted:
            self._write(directory / "cgroup.subtree_control", wanted)

    def create(self, name: str) -> CgroupHandle:
        self.parent.mkdir(parents=True, exist_ok=True)
        self._delegate(self.root)
        self._delegate(self.parent)

        path = self.parent / name
        path.mkdir(exist_ok=True)
        self.log.info("cgroup created | path=%s", path)
        return CgroupHandle(name=name, backend=self.name, path=path)

    def apply(self, handle: CgroupHandle, intent: Intent) -> None:
        if handle.path is None:
            return
        applied: dict[str, str] = {}
        for filename, value in (
            ("memory.high", intent.memory_high_value),
            ("cpu.weight", str(intent.cpu_weight)),
            ("memory.oom.group", "1"),
        ):
            if self._write(handle.path / filename, value):
                applied[filename] = value
        handle.applied = applied
        self.log.info("cgroup limits applied | path=%s %s", handle.path, applied)

    def join_self(self, handle: CgroupHandle) -> None:
        if handle.path is None:
            return
        try:
            (handle.path / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

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


def select_backend(root: Path = CGROUP2_ROOT, force_null: bool = False) -> Any:
    log = get_logger("cgroup")
    if force_null or os.name != "posix":
        return NullBackend()
    backend = Cgroup2Backend(root=root)
    if backend.available():
        return backend
    log.warning("cgroup v2 unavailable at %s, falling back to observation only", root)
    return NullBackend()
