# Engineered by uncoalesced

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from features.analysis.dataset import Run
from features.wrapper import logging_setup
from features.wrapper.schema import Sample, ToolCallRecord


@pytest.fixture(autouse=True)
def clean_logging():
    logging_setup.reset_for_tests()
    logging_setup.configure(to_stderr=False)
    yield
    logging_setup.reset_for_tests()


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    target = tmp_path / "run"
    target.mkdir()
    return target


@pytest.fixture
def make_call():
    def build(
        command: str = "pytest",
        tool_type: str = "Bash",
        start_ts: float = 0.0,
        duration: float = 1.0,
        peak: float = 100.0,
        avg: float = 50.0,
        cpu: float = 10.0,
        task_id: str = "task",
    ) -> ToolCallRecord:
        return ToolCallRecord(
            task_id=task_id,
            tool_type=tool_type,
            command=command,
            start_ts=start_ts,
            end_ts=start_ts + duration,
            peak_memory_mb=peak,
            avg_memory_mb=avg,
            avg_cpu_pct=cpu,
            duration_s=duration,
        )

    return build


@pytest.fixture
def make_samples():
    def build(pairs, cpu: float = 10.0) -> list[Sample]:
        return [
            Sample(t=float(t), mem_mb=float(mem), cpu_pct=float(cpu_value if len(item) > 2 else cpu))
            for item in pairs
            for t, mem, cpu_value in [(item[0], item[1], item[2] if len(item) > 2 else cpu)]
        ]

    return build


@pytest.fixture
def make_run():
    def build(task_id: str = "task", calls=None, samples=None, run_dir: Path = Path("runs/task")) -> Run:
        return Run(
            run_dir=run_dir,
            task_id=task_id,
            toolcalls=sorted(calls or [], key=lambda c: c.start_ts),
            samples=sorted(samples or [], key=lambda s: s.t),
        )

    return build


@pytest.fixture
def busy_child():
    procs: list[subprocess.Popen] = []

    def spawn(duration: float = 5.0) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; buf=bytearray(20*1024*1024); time.sleep({duration})"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if psutil.Process(proc.pid).is_running():
                    break
            except psutil.Error:
                pass
            time.sleep(0.02)
        return proc

    yield spawn

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
