# Engineered by uncoalesced

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from features.wrapper import logging_setup


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
