# Engineered by uncoalesced

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from features.wrapper.sampler import TreeSampler, resolve_agent_root, run_sampler, stop_file
from features.wrapper.schema import SAMPLES_FILENAME, JsonlWriter, read_jsonl


def test_resolve_agent_root_returns_a_live_pid():
    pid = resolve_agent_root()
    assert isinstance(pid, int) and pid > 0


def test_resolve_agent_root_on_dead_pid_falls_back_to_that_pid():
    assert resolve_agent_root(start_pid=2**31 - 1) == 2**31 - 1


def test_sample_once_measures_the_current_process():
    sampler = TreeSampler(root_pid=os.getpid())
    sample = sampler.sample_once()
    assert sample.mem_mb > 0
    assert sample.n_procs >= 1
    assert sample.cpu_pct >= 0


@pytest.mark.integration
def test_sample_once_includes_children(busy_child):
    sampler = TreeSampler(root_pid=os.getpid())
    before = sampler.sample_once()
    busy_child(duration=10.0)
    after = sampler.sample_once()
    assert after.n_procs > before.n_procs
    assert after.mem_mb > before.mem_mb


def test_run_stops_on_max_samples(run_dir: Path):
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        sampler = TreeSampler(root_pid=os.getpid(), interval=0.01, writer=writer)
        written = sampler.run(max_samples=5)
    assert written == 5
    assert len(list(read_jsonl(run_dir / SAMPLES_FILENAME))) == 5


def test_run_stops_on_stop_condition():
    sampler = TreeSampler(root_pid=os.getpid(), interval=0.01)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    assert sampler.run(stop_check=stop) == 3


def test_run_exits_immediately_when_root_is_gone():
    sampler = TreeSampler(root_pid=2**31 - 1, interval=0.01)
    assert sampler.run(max_samples=10) == 0


def test_run_survives_a_failing_tick(monkeypatch):
    sampler = TreeSampler(root_pid=os.getpid(), interval=0.01)
    calls = {"n": 0}
    original = TreeSampler.sample_once

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated psutil explosion")
        return original(self)

    monkeypatch.setattr(TreeSampler, "sample_once", flaky)
    assert sampler.run(max_samples=3) == 3
    assert sampler.failed_samples == 1


def test_run_honours_the_interval(run_dir: Path):
    started = time.monotonic()
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        TreeSampler(root_pid=os.getpid(), interval=0.05, writer=writer).run(max_samples=4)
    assert time.monotonic() - started >= 0.10


@pytest.mark.integration
def test_run_sampler_writes_stream_and_respects_stop_file(run_dir: Path):
    stop_file(run_dir).touch()
    assert run_sampler(run_dir, root_pid=os.getpid(), interval=0.01) == 0

    stop_file(run_dir).unlink()
    written = run_sampler(run_dir, root_pid=os.getpid(), interval=0.01, max_duration_s=0.2)
    assert written > 0
    rows = list(read_jsonl(run_dir / SAMPLES_FILENAME))
    assert len(rows) == written
    assert all(row["mem_mb"] > 0 for row in rows)
