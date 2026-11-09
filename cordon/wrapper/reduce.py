# Engineered by uncoalesced

from __future__ import annotations

import bisect
import json
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cordon.wrapper.logging_setup import get_logger, log_failure
from cordon.wrapper.schema import (
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    INTERACTIVE_TOOLS,
    MARKERS_FILENAME,
    PROCS_FILENAME,
    SAMPLES_FILENAME,
    TOOLCALLS_FILENAME,
    JsonlWriter,
    Marker,
    Sample,
    ToolCallRecord,
    is_cordon_cmd,
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
    # Time the session was in use (idle gaps over IDLE_GAP_S left out); tool share is over this.
    active_s: float = 0.0
    tool_time_s: float = 0.0
    # Tool calls that started and never ended: blocked by another PreToolUse hook, denied at the
    # permission prompt, or interrupted. They never ran, so they have no window.
    unpaired_by_tool: dict[str, int] = field(default_factory=dict)
    # "positive": calls own only their own tool's processes; "window": anything born in the
    # call's window minus hooks; "delta": no per-process data.
    attribution_mode: str = "delta"
    hooks_from: str = ""
    process_groups: list[dict[str, Any]] = field(default_factory=list)
    records: list[ToolCallRecord] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.n_toolcalls == 0

    def to_dict(self) -> dict[str, Any]:
        payload = {k: v for k, v in self.__dict__.items() if k != "records"}
        payload["empty"] = self.empty
        base = self.active_s or self.span_s
        payload["tool_time_fraction"] = round(self.tool_time_s / base, 4) if base else 0.0
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


def pre_call_level(
    samples: list[Sample], times: list[float], start_ts: float, window_s: float = PRE_CALL_WINDOW_S
) -> tuple[float, float] | None:
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
            procs.append(
                {
                    "pid": int(raw["pid"]),
                    "ppid": int(raw.get("ppid", -1)),
                    "created": float(raw["created"]),
                    "cmd": str(raw.get("cmd", "")),
                }
            )
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


HOOK = "hook"
TOOL = "tool"
OTHER = "other"
CORDON = "cordon"
BACKGROUND = "background"

# What each tool's own process looks like when Claude Code starts it. The Bash tool sources a
# shell snapshot, the PowerShell tool runs a launcher script, Grep/Glob run ripgrep. A run that
# contains any of these gets positive attribution: a call owns only trees that look like its
# tool, so peer hooks, MCP servers and the agent's own helpers are never billed to it.
_RG = re.compile(r"^\s*\"?(?:[^\"]*[\\/])?rg(?:\.exe)?\"?\s", re.IGNORECASE)
TOOL_FINGERPRINTS: dict[str, re.Pattern[str]] = {
    # Without a snapshot the Bash tool's shell runs `export TEMP=... && shopt -u extglob ...`.
    "Bash": re.compile(r"shell-snapshots|SNAPSHOT_FILE=|shopt -u extglob"),
    "PowerShell": re.compile(r"__claudeCodeScript|CLAUDE_CODE_SHELL_LAUNCHER"),
    "Grep": _RG,
    "Glob": _RG,
}
# Shell-tool fingerprints are what turn positive attribution on for a run.
_MODE_TOOLS = ("Bash", "PowerShell")
# Claude Code spawns its hooks the moment it sends Cordon the matching event.
HOOK_BURST_S = 1.5
# The agent's own housekeeping shell (PATH probe before the first snapshot).
_AGENT_INTERNAL = re.compile(r"\s-lc\s+echo\s+\"?\$PATH")
# Gaps between hook events longer than this are idle time (the person was away).
IDLE_GAP_S = 600.0
GROUP_LIMIT = 25


def tool_of(cmd: str) -> str | None:
    """The tool whose own process this command line is, if it carries a fingerprint."""
    for tool, pattern in TOOL_FINGERPRINTS.items():
        if pattern.search(cmd):
            return tool
    return None


def uses_positive_attribution(procs: list[dict[str, Any]], tops: list[int]) -> bool:
    return any(tool_of(procs[i].get("cmd", "")) in _MODE_TOOLS for i in set(tops))


def classify_tops(
    procs: list[dict[str, Any]], tops: list[int], matcher: Any, event_times: list[float], positive: bool
) -> dict[int, tuple[str, str]]:
    """(kind, tool-or-label) for each top-level process index: cordon, hook, tool or other."""
    from cordon.wrapper.hookconfig import strip_shell

    kinds: dict[int, tuple[str, str]] = {}
    for index in sorted(set(tops)):
        cmd = procs[index].get("cmd", "")
        tool = tool_of(cmd) if positive else None
        if is_cordon_cmd(cmd):
            kinds[index] = (CORDON, "")
        elif tool is not None:
            kinds[index] = (TOOL, tool)
        elif matcher.is_hook(cmd) or (
            positive
            and not _AGENT_INTERNAL.search(cmd)
            and strip_shell(cmd) != cmd
            and _near(event_times, procs[index]["created"])
        ):
            kinds[index] = (HOOK, matcher.label(cmd))
        else:
            kinds[index] = (OTHER, exe_label(cmd))
    return kinds


def _near(times: list[float], t: float, after_s: float = HOOK_BURST_S) -> bool:
    """Born within after_s of an event (or a hair before it: clocks differ)."""
    i = bisect.bisect_right(times, t + START_SLACK_S)
    return i > 0 and t - times[i - 1] <= after_s


def exe_label(cmd: str) -> str:
    """`C:\\...\\node.exe x.js` -> `node x.js`; plain executables keep just their name."""
    cmd = cmd.strip().removeprefix("\\??\\")
    if cmd.startswith('"'):
        exe, _, rest = cmd[1:].partition('"')
    elif match := re.match(r"(.*?\.exe)(?=\s|$)", cmd, re.IGNORECASE):
        # Unquoted Windows paths keep their spaces (C:\Program Files\...\node.exe).
        exe, rest = match.group(1), cmd[match.end() :]
    else:
        exe, _, rest = cmd.partition(" ")
    name = re.sub(r"\.exe$", "", re.split(r"[\\/]", exe)[-1], flags=re.IGNORECASE)
    if name.lower() not in {"cmd", "npx", "node", "python", "python3", "py", "uv", "uvx", "bash", "sh"}:
        return name
    arg = next((w for w in rest.split() if not w.startswith(("-", "/", "^-"))), "")
    arg = re.split(r"[\\/]", arg.strip("'\"^"))[-1][:30]
    return f"{name} {arg}".strip()


def assign_positive(
    windows: list[tuple[float, float]],
    tool_types: list[str],
    procs: list[dict[str, Any]],
    tops: list[int],
    kinds: dict[int, tuple[str, str]],
    slack_s: float = START_SLACK_S,
) -> list[dict[int, float]]:
    """Each call owns the trees of its own tool born in its window; other calls own nothing."""
    owned: list[dict[int, float]] = [{} for _ in windows]
    starts = sorted(range(len(windows)), key=lambda i: windows[i][0])
    for proc, top_index in zip(procs, tops):
        kind, tool = kinds.get(top_index, (OTHER, ""))
        if kind != TOOL:
            continue
        born = procs[top_index]["created"]
        owner = None
        for i in starts:
            start, upper = windows[i]
            if start - slack_s > born:
                break
            if tool_types[i] == tool and born < upper:
                owner = i
        if owner is not None:
            owned[owner][proc["pid"]] = proc["created"]
    return owned


def _new_group(kind: str, label: str) -> dict[str, Any]:
    return {"kind": kind, "label": label, "procs": 0, "peak_mb": 0.0, "cpu_s": 0.0}


def process_groups(
    samples: list[Sample],
    procs: list[dict[str, Any]],
    tops: list[int],
    kinds: dict[int, tuple[str, str]],
    owned: list[dict[int, float]],
    windows: list[tuple[float, float]],
    commands: list[str],
    limit: int = GROUP_LIMIT,
) -> list[dict[str, Any]]:
    """Memory and CPU no tool call was charged with, grouped by what spent it.

    Hook trees are grouped by hook, the agent's other children by executable, and a tool's own
    processes still running after its call ended (``run_in_background``, a detached training
    job) as background work of that call.
    """
    owner_of = {(pid, created): call for call, own in enumerate(owned) for pid, created in own.items()}

    entries: dict[int, list[tuple[float, str, str, int | None]]] = {}
    for proc, top_index in zip(procs, tops):
        kind, label = kinds.get(top_index, (OTHER, ""))
        if kind == CORDON:
            continue
        call = owner_of.get((proc["pid"], proc["created"]))
        if call is not None:
            kind, label = BACKGROUND, " ".join(commands[call].split())[:60]
        elif kind == TOOL:
            kind, label = OTHER, f"{label} tool, no call"
        entries.setdefault(proc["pid"], []).append((proc["created"], kind, label, call))

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    outlived: set[tuple[int, float]] = set()  # call-owned processes seen after their call ended
    previous_t: float | None = None
    for sample in samples:
        dt = 0.0 if previous_t is None else min(max(sample.t - previous_t, 0.0), 2.0)
        previous_t = sample.t
        mem_now: dict[tuple[str, str], float] = {}
        for pid_text, usage in (sample.kids or {}).items():
            chain = entries.get(int(pid_text)) if pid_text.isdigit() else None
            entry = None
            for item in chain or ():
                if item[0] - BIRTH_SLACK_S <= sample.t:
                    entry = item
            if entry is None or not usage:
                continue
            created, kind, label, call = entry
            if call is not None:
                if windows[call][0] - BIRTH_SLACK_S <= sample.t <= windows[call][1]:
                    continue  # charged to its own call
                outlived.add((int(pid_text), created))
            key = (kind, label)
            mem_now[key] = mem_now.get(key, 0.0) + float(usage[0])
            groups.setdefault(key, _new_group(kind, label))["cpu_s"] += float(usage[1]) / 100.0 * dt
        for key, mem in mem_now.items():
            groups[key]["peak_mb"] = max(groups[key]["peak_mb"], mem)

    for pid, chain in entries.items():
        for created, kind, label, call in chain:
            if call is not None and (pid, created) not in outlived:
                continue  # finished inside its call: nothing left over
            groups.setdefault((kind, label), _new_group(kind, label))["procs"] += 1
    for group in groups.values():
        group["peak_mb"] = round(group["peak_mb"], 1)
        group["cpu_s"] = round(group["cpu_s"], 1)
    ranked = sorted(groups.values(), key=lambda g: (g["cpu_s"], g["peak_mb"], g["procs"]), reverse=True)
    return ranked[:limit]


def active_seconds(event_times: list[float], windows: Iterable[tuple[float, float]], idle_gap_s: float = IDLE_GAP_S) -> float:
    """Time the session was in use: hook events under idle_gap_s apart, plus every tool call.

    A resumed session keeps one run dir, so its wall-clock span can cover days of nobody there.
    """
    times = sorted(event_times)
    spans = [(a, b) for a, b in zip(times, times[1:]) if b - a <= idle_gap_s]
    return union_seconds([*spans, *windows])


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


NO_END_STATUS = "no end event"


def infer_missing_ends(
    markers: list[Marker], pairs: list[tuple[Marker, Marker]], procs: list[dict[str, Any]]
) -> list[tuple[Marker, Marker]]:
    """Close shell calls that ran but never reported an end.

    Before Cordon listened for PostToolUseFailure, a Bash or PowerShell call that failed sent no
    end event at all. Its shell still ran, so when that tool's own process appears after the
    start, the call is closed at the next hook event (the agent's next move). Calls with no
    such process (blocked by another hook, denied) stay unpaired: they never ran.
    """
    paired = {id(start) for start, _ in pairs}
    if not procs:
        return []
    tops = top_levels(procs)
    births: dict[str, list[float]] = {}
    for index in set(tops):
        tool = tool_of(procs[index].get("cmd", ""))
        if tool is not None:
            births.setdefault(tool, []).append(procs[index]["created"])
    for stamps in births.values():
        stamps.sort()
    times = sorted(m.ts for m in markers)

    inferred = []
    for marker in markers:
        if marker.event != EVENT_TOOL_START or id(marker) in paired or marker.tool_type not in births:
            continue
        i = bisect.bisect_right(times, marker.ts)
        end_ts = times[i] if i < len(times) else marker.ts
        stamps = births[marker.tool_type]
        j = bisect.bisect_left(stamps, marker.ts - START_SLACK_S)
        if j < len(stamps) and stamps[j] < end_ts:
            end = Marker(
                event=EVENT_TOOL_END,
                ts=end_ts,
                session_id=marker.session_id,
                call_key=marker.call_key,
                tool_type=marker.tool_type,
                command=marker.command,
                exit_status=NO_END_STATUS,
                agent_pid=marker.agent_pid,
            )
            inferred.append((marker, end))
    return inferred


def reduce_run(run_dir: Path, task_id: str | None = None, write: bool = True) -> ReduceResult:
    log = get_logger("reduce")
    run_dir = Path(run_dir)

    markers, bad_markers = load_markers(run_dir)
    samples = load_samples(run_dir)
    times = [s.t for s in samples]

    session_id = next((m.session_id for m in markers if m.session_id), run_dir.name)
    resolved_task_id = task_id or session_id

    pairs, unpaired, orphan_ends = pair_markers(markers)
    procs = load_procs(run_dir)
    inferred = infer_missing_ends(markers, pairs, procs)
    if inferred:
        pairs = sorted([*pairs, *inferred], key=lambda pair: pair[0].ts)
        unpaired -= len(inferred)
    inferred_starts = {id(start) for start, _ in inferred}

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

    if procs or any(s.kids for s in samples):
        attribute_processes(run_dir, result, markers, samples, times, procs, tool_windows)
    result.n_toolcalls = len(result.records)
    work = [w for w, r in zip(intervals, result.records) if r.tool_type not in INTERACTIVE_TOOLS]
    result.tool_time_s = round(union_seconds(work), 3)
    result.active_s = round(active_seconds([m.ts for m in markers], intervals), 3)
    ends = {m.call_key for m in markers if m.event == EVENT_TOOL_END}
    for marker in markers:
        if marker.event == EVENT_TOOL_START and marker.call_key not in ends and id(marker) not in inferred_starts:
            result.unpaired_by_tool[marker.tool_type] = result.unpaired_by_tool.get(marker.tool_type, 0) + 1

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


def attribute_processes(
    run_dir: Path,
    result: ReduceResult,
    markers: list[Marker],
    samples: list[Sample],
    times: list[float],
    procs: list[dict[str, Any]],
    tool_windows: list[tuple[float, float]],
) -> None:
    """Charge each call for its own processes and group the rest (hooks, background, other)."""
    from cordon.wrapper.hookconfig import HookMatcher, collect, load_snapshot

    hooks = load_snapshot(run_dir)
    result.hooks_from = "session snapshot"
    if hooks is None:
        # Runs from before snapshots: today's config is the best guess at what ran then.
        cwd = next((m.cwd for m in markers if m.cwd), None)
        hooks = collect(cwd)
        result.hooks_from = "current config"
    matcher = HookMatcher(hooks)

    tops = top_levels(procs)
    positive = uses_positive_attribution(procs, tops)
    event_times = sorted(m.ts for m in markers)
    kinds = classify_tops(procs, tops, matcher, event_times, positive)
    if positive:
        result.attribution_mode = "positive"
        owned = assign_positive(tool_windows, [r.tool_type for r in result.records], procs, tops, kinds)
    else:
        result.attribution_mode = "window"
        keep = [i for i, top in enumerate(tops) if kinds.get(top, (OTHER, ""))[0] not in (HOOK, CORDON)]
        kept = [procs[i] for i in keep]
        windows = close_at_post_hook(tool_windows, procs)
        owned = assign_procs(windows, kept)

    for record, own in zip(result.records, owned):
        window = slice_samples(samples, times, record.start_ts, record.end_ts)
        peak, cpu = own_usage(window, own)
        record.own_peak_mb = round(peak, 3)
        record.own_avg_cpu_pct = round(cpu, 2)
        record.own_procs = len(own)
        record.attribution = "subtree"

    windows = [(r.start_ts, r.end_ts) for r in result.records]
    result.process_groups = process_groups(samples, procs, tops, kinds, owned, windows, [r.command for r in result.records])


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
        (run_dir / SUMMARY_FILENAME).write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        log_failure(log, "could not write run summary", run_dir=str(run_dir))
