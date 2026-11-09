# Engineered by uncoalesced
"""Windows backend: one Job Object per guarded command.

A job is Windows' nearest thing to a cgroup: every process the command starts joins it, the
kernel accounts the whole tree's committed memory, and limits apply to the tree as a whole.

What maps and what does not:
- cpu.weight -> weight-based CPU rate control (1..9, 5 is default) plus a priority class
  below normal for low tiers, so a background job yields to the agent and its other calls.
- memory.high has no Windows equivalent: the kernel cannot throttle a job, only refuse its
  allocations. By default the job's memory is watched, not limited (kernel-accounted peak,
  over-limit time as stall, labelled stall_source="job"), which keeps memory.high's promise of
  never killing the command. ``hard_memory=True`` (``control run --hard``) sets JobMemoryLimit
  instead: allocations past the limit fail, and most programs then exit with an error.

The command starts suspended, joins the job, then resumes, so nothing it spawns escapes.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from cordon.control.cgroup import CgroupHandle, CgroupStats, NullBackend
from cordon.control.intent import Intent
from cordon.wrapper.logging_setup import get_logger, log_failure

_BYTES_PER_MB = 1024.0 * 1024.0

CREATE_SUSPENDED = 0x00000004

# winnt.h
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JobObjectMemoryUsageInformation = 28
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED = 0x2
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WIN_WEIGHT_RANGE = (1, 9)
WIN_WEIGHT_DEFAULT = 5


class IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Win32 name
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [("ControlFlags", ctypes.c_uint32), ("Value", ctypes.c_uint32)]


class JOBOBJECT_MEMORY_USAGE_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [("JobMemory", ctypes.c_uint64), ("PeakJobMemoryUsed", ctypes.c_uint64)]


def windows_weight(cpu_weight: int) -> int:
    """cpu.weight (1..10000, 100 default) -> job weight (1..9, 5 default): one step per doubling."""
    step = round(math.log2(max(1, cpu_weight) / 100))
    return max(WIN_WEIGHT_RANGE[0], min(WIN_WEIGHT_RANGE[1], WIN_WEIGHT_DEFAULT + step))


def priority_class(cpu_weight: int) -> int | None:
    """Idle priority for cpu:low and under, below normal under the default, else untouched."""
    if cpu_weight <= 25:
        return IDLE_PRIORITY_CLASS
    if cpu_weight < 100:
        return BELOW_NORMAL_PRIORITY_CLASS
    return None


class Win32Jobs:
    """The handful of kernel32/ntdll calls the backend needs, typed once."""

    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self.ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        k = self.kernel32
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.ntdll.NtResumeProcess.restype = ctypes.c_long

    @staticmethod
    def _check(ok: Any, what: str) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error(), what)  # type: ignore[attr-defined]

    def create_job(self) -> int:
        job = self.kernel32.CreateJobObjectW(None, None)
        self._check(job, "CreateJobObjectW")
        return job

    def set_info(self, job: int, cls: int, struct: ctypes.Structure) -> None:
        ok = self.kernel32.SetInformationJobObject(job, cls, ctypes.byref(struct), ctypes.sizeof(struct))
        self._check(ok, f"SetInformationJobObject({cls})")

    def query_info(self, job: int, cls: int, struct: ctypes.Structure) -> bool:
        return bool(self.kernel32.QueryInformationJobObject(job, cls, ctypes.byref(struct), ctypes.sizeof(struct), None))

    def assign_and_resume(self, job: int, pid: int) -> None:
        access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME | PROCESS_QUERY_LIMITED_INFORMATION
        proc = self.kernel32.OpenProcess(access, False, pid)
        self._check(proc, "OpenProcess")
        try:
            assigned = self.kernel32.AssignProcessToJobObject(job, proc)
            error = None if assigned else ctypes.get_last_error()
            # Resume whatever happened: a command that could not join still has to run.
            self.ntdll.NtResumeProcess(proc)
            if error is not None:
                raise ctypes.WinError(error, "AssignProcessToJobObject")  # type: ignore[attr-defined]
        finally:
            self.kernel32.CloseHandle(proc)

    def close(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)


@dataclass
class _Job:
    handle: int
    limit: int | None
    hard: bool
    attached: bool = False
    last_t: float | None = None
    over: bool = False
    high_events: int = 0
    over_s: float = 0.0
    peak: int = 0


class WindowsJobBackend(NullBackend):
    name = "jobobject"

    def __init__(self, hard_memory: bool = False, win32: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.hard_memory = hard_memory
        self.win32 = win32
        self.clock = clock
        self._jobs: dict[str, _Job] = {}
        self.log = get_logger("cgroup.jobobject")

    def _api(self) -> Any:
        if self.win32 is None:
            self.win32 = Win32Jobs()
        return self.win32

    def usable(self) -> bool:
        """Create a job, set every limit this backend uses, read it back, close it."""
        if sys.platform != "win32" and self.win32 is None:
            return False
        try:
            api = self._api()
            job = api.create_job()
            try:
                self._configure(api, job, Intent(memory_high_bytes=256 * 1024 * 1024, cpu_weight=25), hard=True)
                return api.query_info(job, JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION())
            finally:
                api.close(job)
        except Exception:
            log_failure(self.log, "job objects not usable here")
            return False

    def creationflags(self, handle: CgroupHandle) -> int:
        """Popen flags: start suspended so the command joins the job before it can spawn anything."""
        return CREATE_SUSPENDED if handle.name in self._jobs else 0

    def create(self, name: str) -> CgroupHandle:
        job = self._api().create_job()
        self._jobs[name] = _Job(handle=job, limit=None, hard=self.hard_memory)
        return CgroupHandle(name=name, backend=self.name)

    @staticmethod
    def _configure(api: Any, job: int, intent: Intent, hard: bool) -> dict[str, str]:
        applied: dict[str, str] = {}
        extended = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = 0
        pclass = priority_class(intent.cpu_weight)
        if pclass is not None:
            flags |= JOB_OBJECT_LIMIT_PRIORITY_CLASS
            extended.BasicLimitInformation.PriorityClass = pclass
            applied["priority"] = "idle" if pclass == IDLE_PRIORITY_CLASS else "below_normal"
        if hard and intent.memory_high_bytes is not None:
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            extended.JobMemoryLimit = intent.memory_high_bytes
            applied["job_memory_limit"] = str(intent.memory_high_bytes)
        elif intent.memory_high_bytes is not None:
            applied["memory.high"] = "watch:" + intent.memory_high_value
        extended.BasicLimitInformation.LimitFlags = flags
        api.set_info(job, JobObjectExtendedLimitInformation, extended)

        weight = windows_weight(intent.cpu_weight)
        rate = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
            ControlFlags=JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED, Value=weight
        )
        api.set_info(job, JobObjectCpuRateControlInformation, rate)
        applied["cpu_weight"] = str(weight)
        return applied

    def apply(self, handle: CgroupHandle, intent: Intent) -> None:
        job = self._jobs[handle.name]
        job.limit = intent.memory_high_bytes
        handle.applied = self._configure(self._api(), job.handle, intent, hard=job.hard)
        self.log.info("job limits | name=%s %s", handle.name, handle.applied)

    def bind_pid(self, handle: CgroupHandle, pid: int) -> None:
        handle.pid = pid
        job = self._jobs.get(handle.name)
        if job is None:
            return
        self._api().assign_and_resume(job.handle, pid)
        job.attached = True

    def confirm_membership(self, handle: CgroupHandle) -> bool:
        job = self._jobs.get(handle.name)
        return bool(job and job.attached)

    def _usage(self, job: _Job) -> tuple[int | None, int]:
        """(current committed bytes or None, peak committed bytes) for the whole job."""
        api = self._api()
        usage = JOBOBJECT_MEMORY_USAGE_INFORMATION()
        if api.query_info(job.handle, JobObjectMemoryUsageInformation, usage):
            return int(usage.JobMemory), int(usage.PeakJobMemoryUsed)
        extended = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if api.query_info(job.handle, JobObjectExtendedLimitInformation, extended):
            return None, int(extended.PeakJobMemoryUsed)
        return None, 0

    def read_stats(self, handle: CgroupHandle) -> CgroupStats:
        job = self._jobs.get(handle.name)
        if job is None:
            return CgroupStats(observable=False)
        current, peak = self._usage(job)
        now = self.clock()
        if job.over and job.last_t is not None:
            job.over_s += max(0.0, now - job.last_t)
        over = job.limit is not None and current is not None and current > job.limit
        if over and not job.over:
            job.high_events += 1
        job.over, job.last_t = over, now
        job.peak = max(job.peak, peak, current or 0)
        handle.observed_peak_bytes = job.peak
        # The kernel's peak catches a spike between two polls that the poll itself missed.
        missed = int(job.limit is not None and not job.hard and job.peak > job.limit)
        hit_hard = job.hard and job.limit is not None and job.peak >= job.limit * 0.98
        return CgroupStats(
            peak_memory_mb=round(job.peak / _BYTES_PER_MB, 3),
            memory_stall_s=round(job.over_s, 4),
            high_events=job.high_events or missed,
            max_events=int(hit_hard),
            observable=True,
            stall_source="job",
        )

    def destroy(self, handle: CgroupHandle) -> bool:
        job = self._jobs.pop(handle.name, None)
        if job is not None:
            try:
                # No KILL_ON_JOB_CLOSE: processes the command left running on purpose survive.
                self._api().close(job.handle)
            except Exception:
                log_failure(self.log, "could not close job handle", name=handle.name)
        return True
