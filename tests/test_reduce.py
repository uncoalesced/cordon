# Engineered by uncoalesced

from __future__ import annotations

import json
from pathlib import Path

from features.wrapper.reduce import (
    SUMMARY_FILENAME,
    pair_markers,
    reduce_run,
    slice_samples,
)
from features.wrapper.schema import (
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    JsonlWriter,
    Marker,
    Sample,
    read_jsonl,
)


def _markers(run_dir: Path, markers: list[Marker]) -> None:
    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        for marker in markers:
            writer.write(marker)


def _samples(run_dir: Path, samples: list[Sample]) -> None:
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        for sample in samples:
            writer.write(sample)


def _start(ts: float, key: str = "k1", tool: str = "Bash", command: str = "pytest") -> Marker:
    return Marker(event=EVENT_TOOL_START, ts=ts, session_id="sess", call_key=key, tool_type=tool, command=command)


def _end(ts: float, key: str = "k1", status: str = "0") -> Marker:
    return Marker(event=EVENT_TOOL_END, ts=ts, session_id="sess", call_key=key, exit_status=status)


def test_pair_markers_matches_by_key():
    pairs, unpaired, orphans = pair_markers([_start(1.0, "a"), _start(2.0, "b"), _end(3.0, "b"), _end(4.0, "a")])
    assert [(s.ts, e.ts) for s, e in pairs] == [(1.0, 4.0), (2.0, 3.0)]
    assert unpaired == 0 and orphans == 0


def test_pair_markers_is_lifo_for_repeated_identical_calls():
    pairs, unpaired, orphans = pair_markers([_start(1.0), _start(2.0), _end(3.0)])
    assert pairs[0][0].ts == 2.0
    assert unpaired == 1 and orphans == 0


def test_pair_markers_counts_orphan_ends():
    _, unpaired, orphans = pair_markers([_end(1.0, "ghost")])
    assert unpaired == 0 and orphans == 1


def test_slice_samples_is_inclusive_on_both_bounds():
    samples = [Sample(t=float(i), mem_mb=1.0, cpu_pct=0.0) for i in range(10)]
    times = [s.t for s in samples]
    assert [s.t for s in slice_samples(samples, times, 2.0, 4.0)] == [2.0, 3.0, 4.0]
    assert slice_samples(samples, times, 20.0, 30.0) == []


def test_reduce_run_produces_spec_shaped_records(run_dir: Path):
    _markers(run_dir, [_start(100.0), _end(103.0)])
    _samples(run_dir, [
        Sample(t=99.0, mem_mb=180.0, cpu_pct=2.0),
        Sample(t=100.5, mem_mb=200.0, cpu_pct=10.0),
        Sample(t=101.5, mem_mb=800.0, cpu_pct=90.0),
        Sample(t=102.5, mem_mb=400.0, cpu_pct=50.0),
        Sample(t=110.0, mem_mb=185.0, cpu_pct=1.0),
    ])

    result = reduce_run(run_dir, task_id="task-1")

    assert result.n_toolcalls == 1
    record = result.records[0]
    assert record.task_id == "task-1"
    assert record.tool_type == "Bash"
    assert record.peak_memory_mb == 800.0
    assert record.avg_memory_mb == 466.667
    assert record.avg_cpu_pct == 50.0
    assert record.n_samples == 3
    assert record.duration_s == 3.0
    assert record.exit_status == "0"
    assert [s["t"] for s in record.samples] == [100.5, 101.5, 102.5]


def test_reduce_run_writes_output_files(run_dir: Path):
    _markers(run_dir, [_start(1.0), _end(2.0)])
    _samples(run_dir, [Sample(t=1.5, mem_mb=100.0, cpu_pct=5.0)])

    reduce_run(run_dir)

    rows = list(read_jsonl(run_dir / TOOLCALLS_FILENAME))
    assert len(rows) == 1 and rows[0]["peak_memory_mb"] == 100.0

    summary = json.loads((run_dir / SUMMARY_FILENAME).read_text(encoding="utf-8"))
    assert summary["n_toolcalls"] == 1
    assert summary["task_id"] == "sess"


def test_reduce_run_is_idempotent(run_dir: Path):
    _markers(run_dir, [_start(1.0), _end(2.0)])
    _samples(run_dir, [Sample(t=1.5, mem_mb=100.0, cpu_pct=5.0)])
    reduce_run(run_dir)
    reduce_run(run_dir)
    assert len(list(read_jsonl(run_dir / TOOLCALLS_FILENAME))) == 1


def test_reduce_run_counts_windows_too_short_to_sample(run_dir: Path):
    _markers(run_dir, [_start(1.0), _end(1.1)])
    _samples(run_dir, [Sample(t=0.5, mem_mb=100.0, cpu_pct=5.0), Sample(t=2.0, mem_mb=100.0, cpu_pct=5.0)])

    result = reduce_run(run_dir)

    assert result.empty_windows == 1
    assert result.records[0].n_samples == 0
    assert result.records[0].peak_memory_mb == 0.0


def test_reduce_run_reports_tool_time_fraction_over_the_union(run_dir: Path):
    _markers(run_dir, [_start(0.0, "a"), _start(1.0, "b"), _end(3.0, "b"), _end(2.0, "a")])
    _samples(run_dir, [Sample(t=0.0, mem_mb=1.0, cpu_pct=0.0), Sample(t=6.0, mem_mb=1.0, cpu_pct=0.0)])

    summary = reduce_run(run_dir).to_dict()

    assert summary["span_s"] == 6.0
    assert summary["tool_time_s"] == 3.0
    assert summary["tool_time_fraction"] == 0.5


def test_reduce_run_survives_malformed_lines(run_dir: Path):
    (run_dir / MARKERS_FILENAME).write_text(
        '{"event": "tool_start"}\n' + json.dumps(_start(1.0).to_dict()) + "\n" + json.dumps(_end(2.0).to_dict()) + "\n",
        encoding="utf-8",
    )
    _samples(run_dir, [Sample(t=1.5, mem_mb=50.0, cpu_pct=1.0)])

    result = reduce_run(run_dir)

    assert result.bad_markers == 1
    assert result.n_toolcalls == 1


def test_reduce_run_on_empty_dir_returns_zeroes(run_dir: Path):
    result = reduce_run(run_dir)
    assert result.n_toolcalls == 0
    assert result.n_samples == 0
    assert result.to_dict()["tool_time_fraction"] == 0.0
