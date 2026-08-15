# Engineered by uncoalesced

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import psutil

from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.schema import DEFAULT_INTERVAL_S, SAMPLES_FILENAME, JsonlWriter, Sample

STOP_FILENAME = "STOP"

AGENT_PROCESS_NAMES = {"node", "node.exe", "claude", "claude.exe", "bun", "bun.exe"}

_BYTES_PER_MB = 1024.0 * 1024.0


def resolve_agent_root(start_pid: int | None = None, max_depth: int = 12) -> int:
    log = get_logger("sampler")
    start_pid = os.getpid() if start_pid is None else start_pid

    try:
        current = psutil.Process(start_pid)
    except psutil.Error:
        log_failure(log, "cannot open start process, sampling it by pid anyway", start_pid=start_pid)
        return start_pid

    fallback = start_pid
    for _ in range(max_depth):
        parent = current.parent()
        if parent is None:
            break
        fallback = parent.pid
        try:
            name = parent.name().lower()
        except psutil.Error:
            name = ""
        if name in AGENT_PROCESS_NAMES:
            log.info("resolved agent root by name | pid=%s name=%s", parent.pid, name)
            return parent.pid
        current = parent

    log.warning("no agent process name matched, falling back to ancestor | pid=%s", fallback)
    return fallback


class TreeSampler:
    def __init__(
        self,
        root_pid: int,
        interval: float = DEFAULT_INTERVAL_S,
        writer: JsonlWriter | None = None,
    ) -> None:
        self.root_pid = root_pid
        self.interval = max(0.01, float(interval))
        self.writer = writer
        self.log = get_logger("sampler")
        self.partial_samples = 0
        self.failed_samples = 0
        self._procs: dict[int, psutil.Process] = {}

    def _tree(self) -> list[psutil.Process]:
        # ponytail: children(recursive=True) rescans the full process table each tick.
        # Costs a few ms at 250ms intervals; swap for an exec-event feed if it ever matters.
        try:
            root = psutil.Process(self.root_pid)
        except psutil.Error:
            return []
        try:
            return [root, *root.children(recursive=True)]
        except psutil.Error:
            return [root]

    def _tracked(self, proc: psutil.Process) -> psutil.Process:
        known = self._procs.get(proc.pid)
        if known is not None and known.pid == proc.pid:
            return known
        try:
            proc.cpu_percent(None)
        except psutil.Error:
            pass
        self._procs[proc.pid] = proc
        return proc

    def sample_once(self) -> Sample:
        now = time.time()
        tree = self._tree()
        live_pids = {proc.pid for proc in tree}
        for pid in list(self._procs):
            if pid not in live_pids:
                del self._procs[pid]

        mem_bytes = 0
        cpu_pct = 0.0
        counted = 0
        partial = False

        for proc in tree:
            tracked = self._tracked(proc)
            try:
                mem_bytes += tracked.memory_info().rss
                cpu_pct += tracked.cpu_percent(None)
                counted += 1
            except psutil.Error:
                partial = True

        if partial:
            self.partial_samples += 1

        return Sample(
            t=now,
            mem_mb=round(mem_bytes / _BYTES_PER_MB, 3),
            cpu_pct=round(cpu_pct, 2),
            n_procs=counted,
            partial=partial,
        )

    def root_alive(self) -> bool:
        try:
            return psutil.Process(self.root_pid).is_running()
        except psutil.Error:
            return False

    def run(
        self,
        stop_check: Callable[[], bool] | None = None,
        max_samples: int | None = None,
        max_duration_s: float | None = None,
    ) -> int:
        self.log.info(
            "sampler starting | root_pid=%s interval=%.3fs max_samples=%s max_duration=%s",
            self.root_pid,
            self.interval,
            max_samples,
            max_duration_s,
        )

        started = time.monotonic()
        written = 0
        tick = 0

        try:
            while True:
                if stop_check is not None and stop_check():
                    self.log.info("sampler stopping: stop condition met")
                    break
                if max_samples is not None and written >= max_samples:
                    break
                if max_duration_s is not None and (time.monotonic() - started) >= max_duration_s:
                    break
                if not self.root_alive():
                    self.log.info("sampler stopping: root pid %s exited", self.root_pid)
                    break

                try:
                    sample = self.sample_once()
                    if self.writer is None or self.writer.write(sample):
                        written += 1
                except Exception:
                    self.failed_samples += 1
                    log_failure(
                        self.log,
                        "sample tick failed, continuing",
                        root_pid=self.root_pid,
                        tick=tick,
                        tracked_pids=sorted(self._procs),
                    )

                tick += 1
                deadline = started + tick * self.interval
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            self.log.info("sampler stopping: interrupted")

        self.log.info(
            "sampler stopped | written=%s partial=%s failed=%s",
            written,
            self.partial_samples,
            self.failed_samples,
        )
        return written


def stop_file(run_dir: Path) -> Path:
    return Path(run_dir) / STOP_FILENAME


def run_sampler(
    run_dir: Path,
    root_pid: int,
    interval: float = DEFAULT_INTERVAL_S,
    max_duration_s: float | None = None,
) -> int:
    run_dir = Path(run_dir)
    marker = stop_file(run_dir)
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        sampler = TreeSampler(root_pid=root_pid, interval=interval, writer=writer)
        return sampler.run(stop_check=marker.exists, max_duration_s=max_duration_s)
