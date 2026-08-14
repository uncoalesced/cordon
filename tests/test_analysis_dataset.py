# Engineered by uncoalesced

from __future__ import annotations

import json
from pathlib import Path

from features.analysis.dataset import load_dataset, load_run
from features.wrapper.reduce import SUMMARY_FILENAME, reduce_run
from features.wrapper.schema import (
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    JsonlWriter,
    Marker,
    Sample,
)


def _build_run(run_dir: Path, session: str = "sess", command: str = "pytest -q") -> Path:
    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(Marker(event=EVENT_TOOL_START, ts=1.0, session_id=session, call_key="k", tool_type="Bash", command=command))
        writer.write(Marker(event=EVENT_TOOL_END, ts=3.0, session_id=session, call_key="k", exit_status="0"))
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        for t, mem in [(0.0, 200.0), (1.5, 900.0), (2.5, 400.0), (4.0, 210.0)]:
            writer.write(Sample(t=t, mem_mb=mem, cpu_pct=20.0, n_procs=4))
    reduce_run(run_dir)
    return run_dir


def test_load_run_reads_calls_samples_and_summary(tmp_path: Path):
    run = load_run(_build_run(tmp_path / "sess"))
    assert run is not None
    assert run.task_id == "sess"
    assert len(run.toolcalls) == 1
    assert run.toolcalls[0].command == "pytest -q"
    assert run.toolcalls[0].peak_memory_mb == 900.0
    assert len(run.samples) == 4
    assert run.summary["n_toolcalls"] == 1
    assert run.span_s == 4.0
    assert run.windows == [(1.0, 3.0)]


def test_load_run_skips_a_directory_with_no_reduction(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    assert load_run(tmp_path / "empty") is None


def test_load_run_survives_malformed_records(tmp_path: Path):
    run_dir = _build_run(tmp_path / "sess")
    with (run_dir / TOOLCALLS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write('{"tool_type": "Bash"}\n')
    run = load_run(run_dir)
    assert len(run.toolcalls) == 1


def test_load_run_tolerates_an_unreadable_summary(tmp_path: Path):
    run_dir = _build_run(tmp_path / "sess")
    (run_dir / SUMMARY_FILENAME).write_text("{ not json", encoding="utf-8")
    run = load_run(run_dir)
    assert run.summary == {}
    assert run.task_id == "sess"


def test_load_run_falls_back_to_the_directory_name(tmp_path: Path):
    run_dir = tmp_path / "fallback"
    run_dir.mkdir()
    (run_dir / TOOLCALLS_FILENAME).write_text("", encoding="utf-8")
    assert load_run(run_dir).task_id == "fallback"


def test_load_dataset_accepts_a_single_run_directory(tmp_path: Path):
    runs = load_dataset(_build_run(tmp_path / "sess"))
    assert len(runs) == 1


def test_load_dataset_walks_a_root_of_runs(tmp_path: Path):
    root = tmp_path / "runs"
    for name in ("a", "b"):
        _build_run(root / name, session=name)
    (root / "not-a-run").mkdir()

    runs = load_dataset(root)

    assert [run.task_id for run in runs] == ["a", "b"]


def test_load_dataset_on_a_missing_root_returns_nothing(tmp_path: Path):
    assert load_dataset(tmp_path / "absent") == []


def test_run_span_is_zero_without_enough_samples(tmp_path: Path):
    run_dir = tmp_path / "sparse"
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        writer.write(Sample(t=1.0, mem_mb=100.0, cpu_pct=1.0))
    (run_dir / TOOLCALLS_FILENAME).write_text("", encoding="utf-8")
    (run_dir / SUMMARY_FILENAME).write_text(json.dumps({"task_id": "sparse"}), encoding="utf-8")
    assert load_run(run_dir).span_s == 0.0
