# Engineered by uncoalesced

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.reduce import SUMMARY_FILENAME
from features.wrapper.schema import (
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    Sample,
    ToolCallRecord,
    read_jsonl,
)


@dataclass
class Run:
    run_dir: Path
    task_id: str
    toolcalls: list[ToolCallRecord] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def span_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1].t - self.samples[0].t

    @property
    def windows(self) -> list[tuple[float, float]]:
        return [(c.start_ts, c.end_ts) for c in self.toolcalls]


def load_run(run_dir: Path) -> Run | None:
    log = get_logger("analysis")
    run_dir = Path(run_dir)

    toolcalls_path = run_dir / TOOLCALLS_FILENAME
    if not toolcalls_path.exists():
        log.warning("no reduced tool calls, skipping run | run_dir=%s", run_dir)
        return None

    toolcalls: list[ToolCallRecord] = []
    for raw in read_jsonl(toolcalls_path):
        try:
            toolcalls.append(ToolCallRecord.from_dict(raw))
        except (KeyError, TypeError, ValueError):
            log_failure(log, "skipping malformed tool call", run_dir=str(run_dir), raw=raw)
    toolcalls.sort(key=lambda c: c.start_ts)

    samples: list[Sample] = []
    for raw in read_jsonl(run_dir / SAMPLES_FILENAME):
        try:
            samples.append(Sample.from_dict(raw))
        except (KeyError, TypeError, ValueError):
            log_failure(log, "skipping malformed sample", run_dir=str(run_dir), raw=raw)
    samples.sort(key=lambda s: s.t)

    summary: dict[str, Any] = {}
    summary_path = run_dir / SUMMARY_FILENAME
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log_failure(log, "unreadable run summary, continuing without it", path=str(summary_path))

    task_id = str(summary.get("task_id") or (toolcalls[0].task_id if toolcalls else run_dir.name))

    return Run(run_dir=run_dir, task_id=task_id, toolcalls=toolcalls, samples=samples, summary=summary)


def load_dataset(root: Path) -> list[Run]:
    log = get_logger("analysis")
    root = Path(root)

    if not root.exists():
        log.warning("dataset root does not exist | root=%s", root)
        return []

    if (root / TOOLCALLS_FILENAME).exists():
        single = load_run(root)
        return [single] if single else []

    runs: list[Run] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        loaded = load_run(child)
        if loaded is not None:
            runs.append(loaded)

    log.info("loaded dataset | root=%s runs=%s", root, len(runs))
    return runs
