# Engineered by uncoalesced

from __future__ import annotations

import bisect
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.schema import (
    is_cordon_cmd,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    PROCS_FILENAME,
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    JsonlWriter,
    Marker,
    Sample,
    ToolCallRecord,
    read_jsonl,
)

SUMMARY_FILENAME = "summary.json"
PRE_CALL_WINDOW_S = 1.0
BIRTH_SLACK_S = 0.5
# Clock skew between the hook's time.time() and the OS's process start time.
START_SLACK_S = 0.05


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


def pre_call_level(samples: list[Sample], times: list[float], start_ts: float, window_s: float = PRE_CALL_WINDOW_S) -> tuple[float, float] | None:
    """(memory MB, CPU %) of the agent tree just before a call starts: medians over the last
    window_s seconds, else the last sample before start. None when nothing precedes the call."""
    hi = bisect.bisect_left(times, start_ts)
    if hi == 0:
        return None
    lo = bisect.bisect_left(times, start_ts - window_s)
    before = samples[lo:hi] or samples[hi - 1 : hi]
    return statistics.median(s.mem_mb for s in before), statistics.median(s.cpu_pct for s in before)


def load_procs(run_dir: Path) -> list[dict[str, Any]]:
    procs = []
    for raw in read_jsonl(Path(run_dir) / PROCS_FILENAME, quiet=True):
        try:
            procs.append({
                "pid": int(raw["pid"]),
                "ppid": int(raw.get("ppid", -1)),
                "created": float(raw["created"]),
                "cmd": str(raw.get("cmd", "")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    procs.sort(key=lambda p: p["created"])
    return procs


def top_levels(procs: list[dict[str, Any]]) -> list[int]:
    """Index of each process's outermost recorded ancestor (procs sorted by birth).

    The parent is the latest-born recorded process with the ppid that was born before the child,
    so a recycled pid never adopts processes that are not its own.
    """
    by_pid: dict[int, list[int]] = {}
    top: list[int] = []
    for i, proc in enumerate(procs):
        candidates = by_pid.get(proc.get("ppid", -1), [])
        parent = next((j for j in reversed(candidates) if procs[j]["created"] <= proc["created"]), None)
        top.append(i if parent is None else top[parent])
        by_pid.setdefault(proc["pid"], []).append(i)
    return top


def close_at_post_hook(windows: list[tuple[float, float]], procs: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """End each window at the first Cordon hook process born after the call started.

    That process is our own post hook, spawned the instant the tool finished, alongside every
    other post hook. Runs recorded before tool_end markers carried spawn_ts rely on this alone;
    newer ones keep whichever bound is earlier.
    """
    tops = top_levels(procs)
    births = sorted({procs[t]["created"] for t in set(tops) if is_cordon_cmd(procs[t].get("cmd", ""))})
    closed = []
    for start, upper in windows:
        i = bisect.bisect_right(births, start)
        closed.append((start, min(upper, births[i]) if i < len(births) and births[i] <= upper else upper))
    return closed


def assign_procs(
    windows: list[tuple[float, float]], procs: list[dict[str, Any]], slack_s: float = START_SLACK_S
) -> list[dict[int, float]]:
    """For each call window (tool start, post-hook spawn), {pid: created} of the processes it started.

    The agent spawns its pre-hooks before the tool and its post-hooks the moment the tool ends,
    all as top-level children. So a call owns exactly the process trees whose top-level process
    was born inside its window; hook trees (and everything a hook spawns in the background) fall
    outside it, and Cordon's own trees are skipped by command. With overlapping calls a tree goes
    to the latest call that had started, which concurrent_calls already flags as a guess.
    """
    owned: list[dict[int, float]] = [{} for _ in windows]
    starts = sorted(range(len(windows)), key=lambda i: windows[i][0])
    tops = top_levels(procs)
    for proc, top_index in zip(procs, tops):
        top = procs[top_index]
        if is_cordon_cmd(top.get("cmd", "")):
            continue
        born = top["created"]
        owner = None
        for i in starts:
            start, upper = windows[i]
            if start - slack_s > born:
                break
            if born < upper:
                owner = i
        if owner is not None:
            owned[owner][proc["pid"]] = proc["created"]
    return owned


def own_usage(window: list[Sample], own: dict[int, float]) -> tuple[float, float]:
    """(peak MB, mean CPU %) of just the given processes across a call's samples."""
    if not own or not window:
        return 0.0, 0.0
    mems, cpus = [], []
    for sample in window:
        kids = sample.kids or {}
        mem = cpu = 0.0
        for pid, born in own.items():
            usage = kids.get(str(pid))
            # A pid can be recycled within a run; only count it from its birth on.
            if usage and sample.t >= born - BIRTH_SLACK_S:
                mem += usage[0]
                cpu += usage[1]
        mems.append(mem)
        cpus.append(cpu)
    return max(mems), sum(cpus) / len(cpus)


def count_overlaps(intervals: list[tuple[float, float]]) -> list[int]:
    """For each interval, how many others overlap it. O(n log n + overlaps)."""
    order = sorted(range(len(intervals)), key=lambda i: intervals[i][0])
    counts = [0] * len(intervals)
    for pos, i in enumerate(order):
        _, end = intervals[i]
        for j in order[pos + 1 :]:
            if intervals[j][0] >= end:
                break
            counts[i] += 1
            counts[j] += 1
    return counts


def union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted(intervals)
    if not ordered:
        return 0.0
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + (cur_end - cur_start)


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
    tool_windows: list[tuple[float, float]] = []

    for start, end in pairs:
        try:
            window = slice_samples(samples, times, start.ts, end.ts)
            mems = [s.mem_mb for s in window]
            cpus = [s.cpu_pct for s in window]
            if not window:
                result.empty_windows += 1
            peak = max(mems) if mems else 0.0
            avg_cpu = sum(cpus) / len(cpus) if cpus else 0.0
            # No sample before the call (it opened the session): fall back to its first sample.
            level = pre_call_level(samples, times, start.ts) or ((mems[0], cpus[0]) if window else (0.0, 0.0))

            record = ToolCallRecord(
                task_id=resolved_task_id,
                tool_type=start.tool_type,
                command=start.command,
                start_ts=start.ts,
                end_ts=end.ts,
                peak_memory_mb=round(max(mems), 3) if mems else 0.0,
                avg_memory_mb=round(sum(mems) / len(mems), 3) if mems else 0.0,
                avg_cpu_pct=round(sum(cpus) / len(cpus), 2) if cpus else 0.0,
                # Per-process maps stay in samples.jsonl; copying them into every call bloats this file.
                samples=[{k: v for k, v in s.to_dict().items() if k != "kids"} for s in window],
                call_key=start.call_key,
                duration_s=round(end.ts - start.ts, 4),
                n_samples=len(window),
                exit_status=end.exit_status,
                hook_overhead_ms=start.hook_overhead_ms,
                pre_call_mb=round(level[0], 3),
                delta_peak_mb=round(max(0.0, peak - level[0]), 3) if mems else 0.0,
                pre_call_cpu_pct=round(level[1], 2),
                delta_cpu_pct=round(max(0.0, avg_cpu - level[1]), 2) if cpus else 0.0,
            )
            result.records.append(record)
            intervals.append((start.ts, end.ts))
            tool_windows.append((start.ts, end.spawn_ts if end.spawn_ts > start.ts else end.ts))
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

    for record, overlaps in zip(result.records, count_overlaps(intervals)):
        record.concurrent_calls = overlaps

    procs = load_procs(run_dir)
    if procs or any(s.kids for s in samples):
        tool_windows = close_at_post_hook(tool_windows, procs)
        for record, own in zip(result.records, assign_procs(tool_windows, procs)):
            window = slice_samples(samples, times, record.start_ts, record.end_ts)
            peak, cpu = own_usage(window, own)
            record.own_peak_mb = round(peak, 3)
            record.own_avg_cpu_pct = round(cpu, 2)
            record.own_procs = len(own)
            record.attribution = "subtree"
    result.n_toolcalls = len(result.records)
    result.tool_time_s = round(union_seconds(intervals), 3)

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
