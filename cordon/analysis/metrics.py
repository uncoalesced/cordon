# Engineered by uncoalesced

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from cordon.analysis.dataset import Run
from cordon.wrapper.logging_setup import get_logger, log_failure
from cordon.wrapper.reduce import union_seconds
from cordon.wrapper.schema import INTERACTIVE_TOOLS, Sample, ToolCallRecord

BURST_THRESHOLD_MB = 300.0
BASELINE_QUANTILE = 0.1
RETRY_MIN_LENGTH = 3
# Shell tools: their command is a command line, so it can be categorised and retried.
RETRY_TOOLS = ("Bash", "PowerShell")
HEAVY_CALL_LIMIT = 15

# An executable, optionally behind a path (.venv/Scripts/python.exe, & "C:\...\git.exe").
_EXE = r"^\s*(?:&\s*)?[\"']?(?:[^\s\"']*[\\/])?"
# In priority order: a segment that runs tests is "test" even if it also cd's and greps.
BASH_CATEGORIES: tuple[tuple[str, str], ...] = (
    (
        "test",
        r"\b(pytest|unittest|nosetests|tox|jest|vitest|mocha|go\s+test|cargo\s+test|mvn\s+test|npm\s+(run\s+)?test)\b"
        r"|\btests?[\\/]\S*test_\w+\.py\b|\bInvoke-Pester\b",
    ),
    ("install", r"\b(pip|pip3|uv|poetry|conda|npm|yarn|pnpm|apt|apt-get|brew|cargo)\s+(install|add|ci|sync)\b"),
    ("build", r"\b(make|cmake|ninja|tsc|gcc|g\+\+|clang|cargo\s+build|npm\s+run\s+build|docker\s+build|uv\s+build)\b"),
    ("python", _EXE + r"(python|python3|py|uv\s+run|uvx)(?:\.exe)?\b"),
    ("vcs", _EXE + r"(git|gh|hg|svn)(?:\.exe)?\b"),
    # Polling for something else to finish (a training run, a log line): time spent waiting.
    ("wait", r"\bStart-Sleep\b|(?:^|\s)sleep\s+\d|\btail\s+-[fF]\b|\bGet-Content\b.*\s-Wait\b|\bWait-(?:Process|Job)\b"),
    (
        "filesystem",
        _EXE + r"(ls|dir|cat|type|head|tail|find|grep|rg|wc|mkdir|rm|cp|mv|touch|echo|sed|awk|du|stat|sort|"
        r"Get-ChildItem|Get-Content|Get-Item|Select-String|Test-Path|Copy-Item|Move-Item|Remove-Item|"
        r"New-Item|Set-Content|Out-File|Measure-Object|Select-Object|Format-Table|Write-Output)\b",
    ),
)
# Setup that says nothing about what a command does: cd, env assignments, PowerShell variables.
_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
_SETUP = re.compile(
    rf"^\s*(?:cd|pushd|Set-Location|sl)\s+{_VALUE}\s*$"
    rf"|^\s*export(?:\s+[A-Za-z_]\w*(?:={_VALUE})?)+\s*$"
    rf"|^\s*(?:[A-Za-z_]\w*={_VALUE}\s*)+$"
    rf"|^\s*\$(?:env:)?\w+\s*=.*$",
    re.IGNORECASE,
)
_ENV_PREFIX = re.compile(rf"^\s*(?:export\s+)?(?:[A-Za-z_]\w*={_VALUE}\s+)+")
_KEYWORD_PREFIX = re.compile(r"^\s*(?:do|then|else|time|sudo|nohup|exec)\s+")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_RANK = {name: i for i, (name, _) in enumerate(BASH_CATEGORIES)}


def _mean(values: Sequence[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    try:
        return round(statistics.correlation(xs, ys), 4)
    except statistics.StatisticsError:
        return None


def in_any_window(t: float, windows: Sequence[tuple[float, float]]) -> bool:
    return any(start <= t <= end for start, end in windows)


def _segment_category(segment: str) -> str | None:
    if _SETUP.match(segment):
        return None
    segment = _KEYWORD_PREFIX.sub("", _ENV_PREFIX.sub("", segment))
    for name, pattern in BASH_CATEGORIES:
        if re.search(pattern, segment, flags=re.IGNORECASE):
            return name
    return "other" if segment.strip() else None


def classify_bash(command: str) -> str:
    """Category of a shell command line: the weightiest category among its segments.

    `cd repo && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest` is a test run, not a
    filesystem command; setup segments (cd, exports, PowerShell variables) do not vote.
    """
    found = [c for seg in _SEGMENT_SPLIT.split(command) if (c := _segment_category(seg))]
    known = [c for c in found if c != "other"]
    if known:
        return min(known, key=_RANK.__getitem__)
    return "other"


@dataclass
class ExecutionSplit:
    span_s: float = 0.0
    active_s: float = 0.0
    tool_time_s: float = 0.0
    non_tool_time_s: float = 0.0
    tool_time_fraction: float = 0.0
    non_tool_time_fraction: float = 0.0
    n_toolcalls: int = 0


@dataclass
class MemoryProfile:
    # "unique" (PSS/USS/private bytes, no shared-page double count) when every sample has it,
    # else "rss". The rss_* fields are always RSS so old and new runs stay comparable.
    metric: str = "rss"
    baseline_mb: float = 0.0
    peak_mb: float = 0.0
    avg_mb: float = 0.0
    task_peak_avg_ratio: float = 0.0
    peak_over_baseline_ratio: float = 0.0
    call_peak_avg_ratios: list[float] = field(default_factory=list)
    max_call_peak_avg_ratio: float = 0.0
    max_ratio_command: str = ""
    rss_peak_mb: float = 0.0
    rss_avg_mb: float = 0.0


@dataclass
class BurstProfile:
    threshold_mb: float = BURST_THRESHOLD_MB
    baseline_mb: float = 0.0
    n_samples: int = 0
    n_burst_samples: int = 0
    sample_time_in_tools_fraction: float = 0.0
    bursts_in_tools_fraction: float = 0.0
    max_change_rate_mb_per_s: float = 0.0
    median_burst_duration_s: float = 0.0


@dataclass
class ToolTypeStats:
    tool_type: str
    n_calls: int = 0
    total_time_s: float = 0.0
    time_share: float = 0.0
    mean_duration_s: float = 0.0
    mean_peak_mb: float = 0.0
    max_peak_mb: float = 0.0
    # Added on top of the pre-call level: the part of the tree's memory/CPU this tool caused.
    mean_delta_peak_mb: float = 0.0
    max_delta_peak_mb: float = 0.0
    mean_delta_cpu_pct: float = 0.0


@dataclass
class HeavyCall:
    task_id: str
    tool_type: str
    command: str
    duration_s: float
    pre_call_mb: float
    delta_peak_mb: float
    delta_cpu_pct: float
    concurrent_calls: int
    attribution: str = "delta"
    own_procs: int = 0


@dataclass
class RetryGroup:
    command: str
    length: int
    total_time_s: float
    start_ts: float


@dataclass
class RetryProfile:
    groups: list[RetryGroup] = field(default_factory=list)
    n_groups: int = 0
    longest_group: int = 0
    retry_time_s: float = 0.0
    retry_time_fraction: float = 0.0


@dataclass
class RunMetrics:
    task_id: str
    run_dir: str
    execution: ExecutionSplit = field(default_factory=ExecutionSplit)
    memory: MemoryProfile = field(default_factory=MemoryProfile)
    bursts: BurstProfile = field(default_factory=BurstProfile)
    tool_types: list[ToolTypeStats] = field(default_factory=list)
    bash_categories: list[ToolTypeStats] = field(default_factory=list)
    retries: RetryProfile = field(default_factory=RetryProfile)
    cpu_memory_correlation: float | None = None
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetMetrics:
    n_runs: int = 0
    n_toolcalls: int = 0
    runs: list[RunMetrics] = field(default_factory=list)
    mean_tool_time_fraction: float = 0.0
    mean_task_peak_avg_ratio: float = 0.0
    max_task_peak_avg_ratio: float = 0.0
    outlier_task_id: str = ""
    runs_with_retries_fraction: float = 0.0
    mean_retry_time_fraction: float = 0.0
    correlation_min: float | None = None
    correlation_max: float | None = None
    correlation_mean: float | None = None
    tool_types: list[ToolTypeStats] = field(default_factory=list)
    bash_categories: list[ToolTypeStats] = field(default_factory=list)
    heaviest_calls: list[HeavyCall] = field(default_factory=list)
    mean_bursts_in_tools_fraction: float = 0.0
    mean_sample_time_in_tools_fraction: float = 0.0
    max_change_rate_mb_per_s: float = 0.0
    degraded_runs: int = 0
    # Sessions that never called a tool: counted, left out of every average.
    empty_runs: int = 0
    # Memory/CPU no tool call was charged with (hooks, background work, the agent's helpers),
    # summed over runs by what spent it.
    process_groups: list[dict[str, Any]] = field(default_factory=list)
    # Calls that started and never ended (blocked, denied, interrupted), by tool.
    unpaired_by_tool: dict[str, int] = field(default_factory=dict)
    total_tool_cpu_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def merge_process_groups(runs: Sequence[Run], limit: int = 15) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        for group in run.summary.get("process_groups") or []:
            if not isinstance(group, dict):
                continue
            key = (str(group.get("kind", "")), str(group.get("label", "")))
            entry = merged.setdefault(key, {"kind": key[0], "label": key[1], "procs": 0, "peak_mb": 0.0, "cpu_s": 0.0, "runs": 0})
            entry["procs"] += int(group.get("procs", 0))
            entry["cpu_s"] = round(entry["cpu_s"] + float(group.get("cpu_s", 0.0)), 1)
            entry["peak_mb"] = max(entry["peak_mb"], float(group.get("peak_mb", 0.0)))
            entry["runs"] += 1
    return sorted(merged.values(), key=lambda g: (g["cpu_s"], g["peak_mb"]), reverse=True)[:limit]


def tool_cpu_seconds(calls: Sequence[ToolCallRecord]) -> float:
    """CPU-seconds charged to tool calls (own processes' mean CPU % over each call's duration)."""
    return round(sum(added_cpu(c) / 100.0 * c.duration_s for c in calls if c.tool_type not in INTERACTIVE_TOOLS), 1)


def execution_split(run: Run) -> ExecutionSplit:
    """Tool time against the time the session was in use.

    Runs reduced since active time existed carry it in summary.json; a resumed session's
    wall-clock span can cover days, so it is only the fallback. Calls that wait on the person
    (AskUserQuestion, ExitPlanMode) are their thinking time, not tool time.
    """
    span = run.span_s
    active = float(run.summary.get("active_s") or 0.0) or span
    tool_time = union_seconds([(c.start_ts, c.end_ts) for c in run.toolcalls if c.tool_type not in INTERACTIVE_TOOLS])
    return ExecutionSplit(
        span_s=round(span, 3),
        active_s=round(active, 3),
        tool_time_s=round(tool_time, 3),
        non_tool_time_s=round(max(0.0, active - tool_time), 3),
        tool_time_fraction=_ratio(tool_time, active),
        non_tool_time_fraction=_ratio(max(0.0, active - tool_time), active),
        n_toolcalls=len(run.toolcalls),
    )


def baseline_mb(samples: Sequence[Sample], quantile: float = BASELINE_QUANTILE, unique: bool = False) -> float:
    if not samples:
        return 0.0
    ordered = sorted((s.mem_mb_unique if unique else s.mem_mb) or 0.0 for s in samples)
    return round(ordered[min(int(len(ordered) * quantile), len(ordered) - 1)], 3)


def uses_unique(samples: Sequence[Sample]) -> bool:
    return bool(samples) and all(s.mem_mb_unique is not None for s in samples)


def memory_profile(run: Run) -> MemoryProfile:
    unique = uses_unique(run.samples)
    rss = [s.mem_mb for s in run.samples]
    mems = [s.mem_mb_unique or 0.0 for s in run.samples] if unique else rss
    profile = MemoryProfile(metric="unique" if unique else "rss", baseline_mb=baseline_mb(run.samples, unique=unique))

    if mems:
        profile.rss_peak_mb = round(max(rss), 3)
        profile.rss_avg_mb = _mean(rss)
        profile.peak_mb = round(max(mems), 3)
        profile.avg_mb = _mean(mems)
        profile.task_peak_avg_ratio = _ratio(profile.peak_mb, profile.avg_mb)
        profile.peak_over_baseline_ratio = _ratio(profile.peak_mb, profile.baseline_mb)

    for call in run.toolcalls:
        if call.avg_memory_mb > 0:
            ratio = round(call.peak_memory_mb / call.avg_memory_mb, 4)
            profile.call_peak_avg_ratios.append(ratio)
            if ratio > profile.max_call_peak_avg_ratio:
                profile.max_call_peak_avg_ratio = ratio
                profile.max_ratio_command = call.command

    return profile


def burst_profile(run: Run, threshold_mb: float = BURST_THRESHOLD_MB) -> BurstProfile:
    samples = run.samples
    windows = run.windows
    profile = BurstProfile(threshold_mb=threshold_mb, baseline_mb=baseline_mb(samples), n_samples=len(samples))

    if not samples:
        return profile

    in_tools = [in_any_window(s.t, windows) for s in samples]
    is_burst = [s.mem_mb - profile.baseline_mb > threshold_mb for s in samples]

    profile.n_burst_samples = sum(is_burst)
    profile.sample_time_in_tools_fraction = _ratio(sum(in_tools), len(samples))
    profile.bursts_in_tools_fraction = _ratio(
        sum(1 for burst, inside in zip(is_burst, in_tools) if burst and inside), profile.n_burst_samples
    )

    rates = []
    for previous, current in zip(samples, samples[1:]):
        delta_t = current.t - previous.t
        if delta_t > 0:
            rates.append(abs(current.mem_mb - previous.mem_mb) / delta_t)
    profile.max_change_rate_mb_per_s = round(max(rates), 3) if rates else 0.0

    durations = []
    start_index: int | None = None
    for index, burst in enumerate(is_burst):
        if burst and start_index is None:
            start_index = index
        elif not burst and start_index is not None:
            durations.append(samples[index - 1].t - samples[start_index].t)
            start_index = None
    if start_index is not None:
        durations.append(samples[-1].t - samples[start_index].t)
    profile.median_burst_duration_s = round(statistics.median(durations), 3) if durations else 0.0

    return profile


def _group_stats(groups: dict[str, list[ToolCallRecord]]) -> list[ToolTypeStats]:
    # Shares are of working tool time: a plan waiting 12 minutes for approval is not work.
    total_time = sum(call.duration_s for calls in groups.values() for call in calls if call.tool_type not in INTERACTIVE_TOOLS)
    stats = []
    for name, calls in groups.items():
        durations = [call.duration_s for call in calls]
        peaks = [call.peak_memory_mb for call in calls]
        deltas = [added_mb(call) for call in calls]
        stats.append(
            ToolTypeStats(
                tool_type=name,
                n_calls=len(calls),
                total_time_s=round(sum(durations), 3),
                time_share=0.0 if name in INTERACTIVE_TOOLS else _ratio(sum(durations), total_time),
                mean_duration_s=_mean(durations),
                mean_peak_mb=_mean(peaks),
                max_peak_mb=round(max(peaks), 3) if peaks else 0.0,
                mean_delta_peak_mb=_mean(deltas),
                max_delta_peak_mb=round(max(deltas), 3) if deltas else 0.0,
                mean_delta_cpu_pct=_mean([added_cpu(call) for call in calls]),
            )
        )
    stats.sort(key=lambda s: (s.tool_type not in INTERACTIVE_TOOLS, s.total_time_s), reverse=True)
    return stats


def added_mb(call: ToolCallRecord) -> float:
    """Memory the call itself is charged with: its own processes when the run recorded them,
    else its rise above the pre-call level of the whole tree."""
    return call.own_peak_mb if call.attribution == "subtree" else call.delta_peak_mb


def added_cpu(call: ToolCallRecord) -> float:
    return call.own_avg_cpu_pct if call.attribution == "subtree" else call.delta_cpu_pct


def heaviest_calls(calls: Sequence[ToolCallRecord], limit: int = HEAVY_CALL_LIMIT) -> list[HeavyCall]:
    """Calls that added the most memory above their pre-call level. Tools that only wait on the
    user are skipped: their windows span minutes of unrelated agent activity."""
    ranked = sorted(
        (call for call in calls if call.tool_type not in INTERACTIVE_TOOLS and added_mb(call) > 0),
        key=added_mb,
        reverse=True,
    )
    return [
        HeavyCall(
            task_id=call.task_id,
            tool_type=call.tool_type,
            command=call.command,
            duration_s=call.duration_s,
            pre_call_mb=call.pre_call_mb,
            delta_peak_mb=added_mb(call),
            delta_cpu_pct=added_cpu(call),
            concurrent_calls=call.concurrent_calls,
            attribution=call.attribution,
            own_procs=call.own_procs,
        )
        for call in ranked[:limit]
    ]


def tool_type_breakdown(calls: Sequence[ToolCallRecord]) -> list[ToolTypeStats]:
    groups: dict[str, list[ToolCallRecord]] = {}
    for call in calls:
        groups.setdefault(call.tool_type or "unknown", []).append(call)
    return _group_stats(groups)


def bash_category_breakdown(calls: Sequence[ToolCallRecord], tools: Sequence[str] = RETRY_TOOLS) -> list[ToolTypeStats]:
    groups: dict[str, list[ToolCallRecord]] = {}
    for call in calls:
        if call.tool_type in tools:
            groups.setdefault(classify_bash(call.command), []).append(call)
    return _group_stats(groups)


def retry_profile(
    calls: Sequence[ToolCallRecord],
    tools: Sequence[str] = RETRY_TOOLS,
    min_length: int = RETRY_MIN_LENGTH,
    ignore_tools: Sequence[str] = (),
) -> RetryProfile:
    sequence = [c for c in sorted(calls, key=lambda c: c.start_ts) if c.tool_type not in ignore_tools]
    profile = RetryProfile()

    index = 0
    while index < len(sequence):
        current = sequence[index]
        if current.tool_type not in tools:
            index += 1
            continue
        end = index + 1
        while end < len(sequence) and sequence[end].tool_type == current.tool_type and sequence[end].command == current.command:
            end += 1
        length = end - index
        if length >= min_length:
            members = sequence[index:end]
            profile.groups.append(
                RetryGroup(
                    command=current.command,
                    length=length,
                    total_time_s=round(sum(c.duration_s for c in members), 3),
                    start_ts=current.start_ts,
                )
            )
        index = end

    profile.n_groups = len(profile.groups)
    profile.longest_group = max((g.length for g in profile.groups), default=0)
    profile.retry_time_s = round(sum(g.total_time_s for g in profile.groups), 3)
    total_tool_time = sum(c.duration_s for c in calls)
    profile.retry_time_fraction = _ratio(profile.retry_time_s, total_tool_time)
    return profile


def analyze_run(run: Run, burst_threshold_mb: float = BURST_THRESHOLD_MB) -> RunMetrics:
    log = get_logger("analysis")
    metrics = RunMetrics(task_id=run.task_id, run_dir=str(run.run_dir))

    try:
        metrics.execution = execution_split(run)
        metrics.memory = memory_profile(run)
        metrics.bursts = burst_profile(run, burst_threshold_mb)
        metrics.tool_types = tool_type_breakdown(run.toolcalls)
        metrics.bash_categories = bash_category_breakdown(run.toolcalls)
        metrics.retries = retry_profile(run.toolcalls)
        metrics.cpu_memory_correlation = _correlation([s.mem_mb for s in run.samples], [s.cpu_pct for s in run.samples])
    except Exception:
        metrics.degraded = True
        log_failure(
            log,
            "run analysis degraded, partial metrics kept",
            task_id=run.task_id,
            run_dir=str(run.run_dir),
            n_toolcalls=len(run.toolcalls),
            n_samples=len(run.samples),
        )

    return metrics


def analyze_dataset(runs: Sequence[Run], burst_threshold_mb: float = BURST_THRESHOLD_MB) -> DatasetMetrics:
    empty = [run for run in runs if not run.toolcalls]
    runs = [run for run in runs if run.toolcalls] or list(runs)
    per_run = [analyze_run(run, burst_threshold_mb) for run in runs]
    all_calls = [call for run in runs for call in run.toolcalls]

    unpaired: dict[str, int] = {}
    for run in runs:
        for tool, count in (run.summary.get("unpaired_by_tool") or {}).items():
            unpaired[tool] = unpaired.get(tool, 0) + int(count)

    dataset = DatasetMetrics(
        n_runs=len(per_run),
        n_toolcalls=len(all_calls),
        runs=per_run,
        tool_types=tool_type_breakdown(all_calls),
        bash_categories=bash_category_breakdown(all_calls),
        heaviest_calls=heaviest_calls(all_calls),
        degraded_runs=sum(1 for m in per_run if m.degraded),
        # When every run is empty they stay in (a one-session report still renders).
        empty_runs=len(empty) if all_calls else 0,
        process_groups=merge_process_groups(runs),
        unpaired_by_tool=dict(sorted(unpaired.items(), key=lambda kv: -kv[1])),
        total_tool_cpu_s=tool_cpu_seconds(all_calls),
    )

    if not per_run:
        return dataset

    dataset.mean_tool_time_fraction = _mean([m.execution.tool_time_fraction for m in per_run])
    dataset.mean_sample_time_in_tools_fraction = _mean([m.bursts.sample_time_in_tools_fraction for m in per_run])
    dataset.max_change_rate_mb_per_s = max(m.bursts.max_change_rate_mb_per_s for m in per_run)

    with_bursts = [m.bursts.bursts_in_tools_fraction for m in per_run if m.bursts.n_burst_samples]
    dataset.mean_bursts_in_tools_fraction = _mean(with_bursts)

    ratios = [m.memory.task_peak_avg_ratio for m in per_run if m.memory.task_peak_avg_ratio]
    if ratios:
        dataset.mean_task_peak_avg_ratio = _mean(ratios)
        dataset.max_task_peak_avg_ratio = round(max(ratios), 4)
        dataset.outlier_task_id = max(per_run, key=lambda m: m.memory.task_peak_avg_ratio).task_id

    dataset.runs_with_retries_fraction = _ratio(sum(1 for m in per_run if m.retries.n_groups), len(per_run))
    dataset.mean_retry_time_fraction = _mean([m.retries.retry_time_fraction for m in per_run])

    correlations = [m.cpu_memory_correlation for m in per_run if m.cpu_memory_correlation is not None]
    if correlations:
        dataset.correlation_min = round(min(correlations), 4)
        dataset.correlation_max = round(max(correlations), 4)
        dataset.correlation_mean = _mean(correlations)

    return dataset
