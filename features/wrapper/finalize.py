# Engineered by uncoalesced

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from features.analysis.dataset import load_dataset, load_run
from features.analysis.metrics import analyze_dataset
from features.analysis.report import render_report
from features.wrapper.hook import sampler_running
from features.wrapper.logging_setup import configure, get_logger, log_failure
from features.wrapper.reduce import reduce_run
from features.wrapper.sampler import (
    DEFAULT_IDLE_STOP_S,
    ENV_IDLE_STOP,
    env_float,
    agent_pid_path,
    idle_for,
    process_identity,
    read_pid_file,
    stop_file,
)
from features.wrapper.schema import (
    EVENT_TOOL_END,
    MARKERS_FILENAME,
    RUN_LOG_FILENAME,
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    read_jsonl,
)

REPORT_FILENAME = "report.md"
SAMPLER_WAIT_S = 5.0


def report_path(run_dir: Path) -> Path:
    return Path(run_dir) / REPORT_FILENAME


def wait_for_sampler(run_dir: Path, timeout_s: float = SAMPLER_WAIT_S, poll_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while sampler_running(run_dir):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)
    return True


def write_run_report(run_dir: Path) -> Path:
    """Reduce one run and write its report.md. Safe to re-run; each call regenerates."""
    run_dir = Path(run_dir)
    reduce_run(run_dir)
    run = load_run(run_dir)
    dataset = analyze_dataset([run] if run is not None else [])
    out = report_path(run_dir)
    out.write_text(render_report(dataset, title=f"Cordon session report: {run_dir.name}"), encoding="utf-8")
    return out


def finalize(run_dir: Path, wait_s: float = SAMPLER_WAIT_S) -> Path | None:
    run_dir = Path(run_dir)
    configure(log_path=run_dir / RUN_LOG_FILENAME, to_stderr=False)
    log = get_logger("finalize")
    if not wait_for_sampler(run_dir, wait_s):
        # Report on what is on disk; the samples written so far are already flushed.
        log.warning("sampler still running after %.1fs, reporting anyway | run_dir=%s", wait_s, run_dir)
    try:
        out = write_run_report(run_dir)
    except Exception:
        log_failure(log, "finalize failed", run_dir=str(run_dir))
        return None
    log.info("report written | path=%s", out)
    return out


def list_runs(runs_root: Path) -> list[Path]:
    """Run dirs under a root, oldest first by last marker write."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    runs = [p for p in root.iterdir() if (p / MARKERS_FILENAME).exists()]
    return sorted(runs, key=lambda p: (p / MARKERS_FILENAME).stat().st_mtime)


def render_all(runs_root: Path) -> str:
    """Aggregate report over every run, reducing any that were never reduced."""
    for run_dir in list_runs(runs_root):
        if not (run_dir / TOOLCALLS_FILENAME).exists():
            reduce_run(run_dir)
    return render_report(analyze_dataset(load_dataset(Path(runs_root))), title="Cordon report: all sessions")


@dataclass
class RunStatus:
    session: str
    run_dir: Path
    started: float | None
    duration_s: float
    n_calls: int
    peak_mb: float | None
    has_report: bool
    sampler_live: bool
    agent_pid: int


def run_status(run_dir: Path) -> RunStatus:
    run_dir = Path(run_dir)
    stamps: list[float] = []
    n_calls = 0
    agent_pid = 0
    for raw in read_jsonl(run_dir / MARKERS_FILENAME):
        try:
            stamps.append(float(raw["ts"]))
        except (KeyError, TypeError, ValueError):
            continue
        n_calls += raw.get("event") == EVENT_TOOL_END
        agent_pid = int(raw.get("agent_pid") or agent_pid)
    cached = read_pid_file(agent_pid_path(run_dir))
    # ponytail: scans the whole samples file for the peak; fine for status, cache it if runs grow huge.
    peak = None
    if (run_dir / SAMPLES_FILENAME).exists():
        peak = max((float(raw.get("mem_mb", 0.0)) for raw in read_jsonl(run_dir / SAMPLES_FILENAME)), default=None)
    return RunStatus(
        session=run_dir.name,
        run_dir=run_dir,
        started=min(stamps) if stamps else None,
        duration_s=(max(stamps) - min(stamps)) if stamps else 0.0,
        n_calls=n_calls,
        peak_mb=peak,
        has_report=report_path(run_dir).exists(),
        sampler_live=sampler_running(run_dir),
        agent_pid=cached[0] if cached else agent_pid,
    )


def collect_status(runs_root: Path) -> list[RunStatus]:
    return [run_status(run_dir) for run_dir in list_runs(runs_root)]


def render_status(rows: list[RunStatus]) -> str:
    if not rows:
        return "no runs recorded"
    header = ("session", "started", "duration", "tool calls", "peak MB", "report", "sampler")
    lines = [
        (
            row.session[:36],
            datetime.fromtimestamp(row.started).strftime("%Y-%m-%d %H:%M") if row.started else "-",
            f"{row.duration_s:.0f}s",
            str(row.n_calls),
            f"{row.peak_mb:.0f}" if row.peak_mb is not None else "-",
            "yes" if row.has_report else "no",
            "live" if row.sampler_live else "no",
        )
        for row in rows
    ]
    widths = [max(len(r[i]) for r in (header, *lines)) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in (header, *lines))


def clean_orphans(runs_root: Path, rows: list[RunStatus] | None = None, now: float | None = None) -> list[str]:
    """Touch STOP for live samplers whose agent is gone or that have seen no markers for the idle limit."""
    rows = collect_status(runs_root) if rows is None else rows
    idle_limit = env_float(ENV_IDLE_STOP, DEFAULT_IDLE_STOP_S)
    stopped = []
    for row in rows:
        if not row.sampler_live:
            continue
        root_dead = not row.agent_pid or process_identity(row.agent_pid) is None
        if root_dead or idle_for(row.run_dir, 0.0, now) > idle_limit:
            stop_file(row.run_dir).touch()
            stopped.append(row.session)
    return stopped