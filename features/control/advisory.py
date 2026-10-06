# Engineered by uncoalesced

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from features import host
from features.control.cgroup import CgroupHandle, CgroupStats, NullBackend
from features.control.intent import Intent
from features.control.probe import TASKPOLICY
from features.wrapper.logging_setup import get_logger

# Soft enforcement for hosts without a usable cgroup v2: macOS always, Linux as the last resort
# (WSL1, proot, locked containers). CPU share becomes a nice value; memory.high becomes a
# user-space watchdog that only measures. Nothing is throttled or killed, matching memory.high's
# throttle-not-kill contract as closely as user space can, and the over-limit seconds feed the
# same FeedbackPolicy the PSI numbers do, labelled stall_source="watchdog".

NICE_MAX = 19
BACKGROUND_WEIGHT = 25  # cpu:low and below run under taskpolicy -b on macOS
_BYTES_PER_MB = 1024.0 * 1024.0

MemoryReader = Callable[[int], "int | None"]


def nice_for_weight(weight: int) -> int:
    """cpu.weight 100 -> nice 0, 50 -> 5, 25 -> 10; five nice steps per halving, clamped 0..19.

    Weights above the default cannot become negative nice values: that needs privilege, and an
    unprivileged agent asking for cpu:high should not fail to start because of it.
    """
    if weight >= 100:
        return 0
    return max(0, min(NICE_MAX, round(5 * math.log2(100 / max(1, weight)))))


def _linux_pss(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    try:
        text = (proc_root / str(pid) / "smaps_rollup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Pss:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def tree_memory_bytes(pid: int, os_kind: str | None = None) -> int | None:
    """Memory of pid and its descendants: PSS on Linux, USS on macOS, RSS where those fail.

    None means the root process is gone (or nothing can be measured), not zero usage.
    """
    os_kind = host.OS if os_kind is None else os_kind
    try:
        import psutil  # lazy: probing must work on hosts where psutil does not build
    except ImportError:
        # ponytail: without psutil only the root pid is visible on Linux; add a /proc walk if needed.
        return _linux_pss(pid) if os_kind == host.LINUX else None

    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None

    total = 0
    for proc in procs:
        size = _linux_pss(proc.pid) if os_kind == host.LINUX else None
        if size is None and os_kind == host.DARWIN:
            try:
                size = int(proc.memory_full_info().uss)
            except (psutil.Error, AttributeError, OSError):
                size = None
        if size is None:
            try:
                size = int(proc.memory_info().rss)
            except psutil.Error:
                size = 0
        total += size
    return total


@dataclass
class _Watch:
    limit: int | None
    last_t: float | None = None
    over: bool = False
    high_events: int = 0
    over_s: float = 0.0
    peak: int = 0
    stats: CgroupStats | None = None


class AdvisoryBackend(NullBackend):
    name = "advisory"

    def __init__(
        self,
        os_kind: str | None = None,
        memory_reader: MemoryReader | None = None,
        clock: Callable[[], float] = time.monotonic,
        taskpolicy: str = TASKPOLICY,
    ) -> None:
        super().__init__()
        self.os_kind = host.OS if os_kind is None else os_kind
        self.memory_reader = memory_reader or (lambda pid: tree_memory_bytes(pid, self.os_kind))
        self.clock = clock
        self.taskpolicy = taskpolicy
        self._watch: dict[str, _Watch] = {}
        self.log = get_logger("cgroup.advisory")

    def create(self, name: str) -> CgroupHandle:
        return CgroupHandle(name=name, backend=self.name)

    def apply(self, handle: CgroupHandle, intent: Intent) -> None:
        applied = {
            "nice": str(nice_for_weight(intent.cpu_weight)),
            "memory.high": "watchdog:" + intent.memory_high_value,
        }
        if self.os_kind == host.DARWIN and intent.cpu_weight <= BACKGROUND_WEIGHT and os.path.exists(self.taskpolicy):
            applied["taskpolicy"] = "-b"
        handle.applied = applied
        self._watch[handle.name] = _Watch(limit=intent.memory_high_bytes)
        self.log.info("advisory limits | name=%s %s", handle.name, applied)

    def wrap_argv(self, handle: CgroupHandle, argv: list[str]) -> list[str]:
        if handle.applied.get("taskpolicy") == "-b":
            # Background QoS: throttled I/O and, on Apple Silicon, efficiency cores only.
            return [self.taskpolicy, "-b", "--", *argv]
        return argv

    def join_self(self, handle: CgroupHandle) -> None:
        # In the child before exec. Only ever raises niceness: lowering it needs privilege.
        target = int(handle.applied.get("nice", "0"))
        current = os.getpriority(os.PRIO_PROCESS, 0)
        if target > current:
            os.setpriority(os.PRIO_PROCESS, 0, target)

    def confirm_membership(self, handle: CgroupHandle) -> bool:
        return handle.pid > 0

    def read_stats(self, handle: CgroupHandle) -> CgroupStats:
        watch = self._watch.setdefault(handle.name, _Watch(limit=None))
        size = self.memory_reader(handle.pid) if handle.pid else None
        if size is None:
            # Process tree already gone: report what was seen while it ran.
            return watch.stats or CgroupStats(observable=False, stall_source="watchdog")

        now = self.clock()
        if watch.over and watch.last_t is not None:
            watch.over_s += max(0.0, now - watch.last_t)
        over = watch.limit is not None and size > watch.limit
        if over and not watch.over:
            watch.high_events += 1
        watch.over = over
        watch.last_t = now
        watch.peak = max(watch.peak, size)
        handle.observed_peak_bytes = watch.peak

        watch.stats = CgroupStats(
            peak_memory_mb=round(watch.peak / _BYTES_PER_MB, 3),
            memory_stall_s=round(watch.over_s, 4),
            high_events=watch.high_events,
            observable=True,
            stall_source="watchdog",
        )
        return watch.stats

    def destroy(self, handle: CgroupHandle) -> bool:
        self._watch.pop(handle.name, None)
        return True

