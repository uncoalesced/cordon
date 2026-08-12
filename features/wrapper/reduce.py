# Engineered by uncoalesced

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.schema import (
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    JsonlWriter,
    Marker,
    Sample,
    ToolCallRecord,
    read_jsonl,
)

SUMMARY_FILENAME = "summary.json"


@dataclass
class ReduceResult:
    run_dir: str
    task_id: str
    n_toolcalls: int = 0
    n_samples: int = 0
    unpaired_starts: int = 0
    orphan_ends: int = 0
    empty_windows: int = 0
    partial_samples: int = 0
    bad_markers: int = 0
    span_s: float = 0.0
    tool_time_s: float = 0.0
    records: list[ToolCallRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {k: v for k, v in self.__dict__.items() if k != "records"}
        payload["tool_time_fraction"] = round(self.tool_time_s / self.span_s, 4) if self.span_s else 0.0
        return payload


def load_markers(run_dir: Path) -> tuple[list[Marker], int]:
    log = get_logger("reduce")
    markers: list[Marker] = []
    bad = 0
    for raw in read_jsonl(Path(run_dir) / MARKERS_FILENAME):
        try:
            markers.append(Marker.from_dict(raw))
        except (KeyError, TypeError, ValueError):
            bad += 1
            log_failure(log, "skipping malformed marker", run_dir=str(run_dir), raw=raw)
    markers.sort(key=lambda m: m.ts)
    return markers, bad


def load_samples(run_dir: Path) -> list[Sample]:
    log = get_logger("reduce")
    samples: list[Sample] = []
    for raw in read_jsonl(Path(run_dir) / SAMPLES_FILENAME):
        try:
            samples.append(Sample.from_dict(raw))
        except (KeyError, TypeError, ValueError):
            log_failure(log, "skipping malformed sample", run_dir=str(run_dir), raw=raw)
    samples.sort(key=lambda s: s.t)
    return samples


def pair_markers(markers: list[Marker]) -> tuple[list[tuple[Marker, Marker]], int, int]:
    pending: dict[str, list[Marker]] = {}
    pairs: list[tuple[Marker, Marker]] = []
    orphan_ends = 0

    for marker in markers:
        if marker.event == EVENT_TOOL_START:
            pending.setdefault(marker.call_key, []).append(marker)
        elif marker.event == EVENT_TOOL_END:
            queue = pending.get(marker.call_key)
            if queue:
                pairs.append((queue.pop(), marker))
            else:
                orphan_ends += 1

    unpaired = sum(len(queue) for queue in pending.values())
    pairs.sort(key=lambda pair: pair[0].ts)
    return pairs, unpaired, orphan_ends


def slice_samples(samples: list[Sample], times: list[float], start_ts: float, end_ts: float) -> list[Sample]:
    lo = bisect.bisect_left(times, start_ts)
    hi = bisect.bisect_right(times, end_ts)
    return samples[lo:hi]


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    merged_total = 0.0
    ordered = sorted(intervals)
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            merged_total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    merged_total += cur_end - cur_start
    return merged_total


def reduce_run(run_dir: Path, task_id: str | None = None, write: bool = True) -> ReduceResult:
    log = get_logger("reduce")
    run_dir = Path(run_dir)

    markers, bad_markers = load_markers(run_dir)
    samples = load_samples(run_dir)
    times = [s.t for s in samples]

    session_id = next((m.session_id for m in markers if m.session_id), run_dir.name)
    resolved_task_id = task_id or session_id

    pairs, unpaired, orphan_ends = pair_markers(markers)

    result = ReduceResult(
        run_dir=str(run_dir),
        task_id=resolved_task_id,
        n_samples=len(samples),
        unpaired_starts=unpaired,
        orphan_ends=orphan_ends,
        partial_samples=sum(1 for s in samples if s.partial),
        bad_markers=bad_markers,
        span_s=round(times[-1] - times[0], 3) if len(times) >= 2 else 0.0,
    )

    intervals: list[tuple[float, float]] = []

    for start, end in pairs:
        try:
            window = slice_samples(samples, times, start.ts, end.ts)
            mems = [s.mem_mb for s in window]
            cpus = [s.cpu_pct for s in window]
            if not window:
                result.empty_windows += 1

            record = ToolCallRecord(
                task_id=resolved_task_id,
                tool_type=start.tool_type,
                command=start.command,
                start_ts=start.ts,
                end_ts=end.ts,
                peak_memory_mb=round(max(mems), 3) if mems else 0.0,
                avg_memory_mb=round(sum(mems) / len(mems), 3) if mems else 0.0,
                avg_cpu_pct=round(sum(cpus) / len(cpus), 2) if cpus else 0.0,
                samples=[s.to_dict() for s in window],
                call_key=start.call_key,
                duration_s=round(end.ts - start.ts, 4),
                n_samples=len(window),
                exit_status=end.exit_status,
                hook_overhead_ms=start.hook_overhead_ms,
            )
            result.records.append(record)
            intervals.append((start.ts, end.ts))
        except Exception:
            log_failure(
                log,
                "reducing tool call failed, skipping it",
                run_dir=str(run_dir),
                call_key=start.call_key,
                tool_type=start.tool_type,
                start_ts=start.ts,
                end_ts=end.ts,
            )

    result.n_toolcalls = len(result.records)
    result.tool_time_s = round(_union_seconds(intervals), 3)

    if write:
        _write_outputs(run_dir, result)

    log.info(
        "reduced run | toolcalls=%s samples=%s unpaired=%s orphans=%s empty=%s",
        result.n_toolcalls,
        result.n_samples,
        result.unpaired_starts,
        result.orphan_ends,
        result.empty_windows,
    )
    return result


def _write_outputs(run_dir: Path, result: ReduceResult) -> None:
    log = get_logger("reduce")
    out_path = run_dir / TOOLCALLS_FILENAME
    try:
        out_path.unlink(missing_ok=True)
    except OSError:
        log_failure(log, "could not clear previous reduction", path=str(out_path))

    with JsonlWriter(out_path) as writer:
        for record in result.records:
            writer.write(record)

    try:
        (run_dir / SUMMARY_FILENAME).write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        log_failure(log, "could not write run summary", run_dir=str(run_dir))
