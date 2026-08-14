# Engineered by uncoalesced

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from features.analysis.dataset import Run
from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.schema import Sample, ToolCallRecord

BURST_THRESHOLD_MB = 300.0
BASELINE_QUANTILE = 0.1
RETRY_MIN_LENGTH = 3
RETRY_TOOLS = ("Bash",)

BASH_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("test", r"\b(pytest|unittest|nosetests|tox|jest|vitest|mocha|go\s+test|cargo\s+test|mvn\s+test|npm\s+(run\s+)?test)\b"),
    ("install", r"\b(pip|pip3|uv|poetry|conda|npm|yarn|pnpm|apt|apt-get|brew|cargo)\s+(install|add|ci)\b"),
    ("build", r"\b(make|cmake|ninja|tsc|gcc|g\+\+|clang|cargo\s+build|npm\s+run\s+build|docker\s+build)\b"),
    ("vcs", r"^\s*(git|gh|hg|svn)\b"),
    ("python", r"^\s*(python|python3|py)\b"),
    ("filesystem", r"^\s*(ls|dir|cat|type|head|tail|find|grep|rg|wc|cd|mkdir|rm|cp|mv|touch|echo|sed|awk)\b"),
)


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


def in_any_window(t: float, windows: Sequence[tuple[float, float]]) -> bool:
    return any(start <= t <= end for start, end in windows)


def classify_bash(command: str) -> str:
    for name, pattern in BASH_CATEGORIES:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return name
    return "other"


@dataclass
class ExecutionSplit:
    span_s: float = 0.0
    tool_time_s: float = 0.0
    non_tool_time_s: float = 0.0
    tool_time_fraction: float = 0.0
    non_tool_time_fraction: float = 0.0
    n_toolcalls: int = 0


@dataclass
class MemoryProfile:
    baseline_mb: float = 0.0
    peak_mb: float = 0.0
    avg_mb: float = 0.0
    task_peak_avg_ratio: float = 0.0
    peak_over_baseline_ratio: float = 0.0
    call_peak_avg_ratios: list[float] = field(default_factory=list)
    max_call_peak_avg_ratio: float = 0.0
    max_ratio_command: str = ""


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
    mean_bursts_in_tools_fraction: float = 0.0
    mean_sample_time_in_tools_fraction: float = 0.0
    max_change_rate_mb_per_s: float = 0.0
    degraded_runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def execution_split(run: Run) -> ExecutionSplit:
    span = run.span_s
    tool_time = union_seconds(run.windows)
    return ExecutionSplit(
        span_s=round(span, 3),
        tool_time_s=round(tool_time, 3),
        non_tool_time_s=round(max(0.0, span - tool_time), 3),
        tool_time_fraction=_ratio(tool_time, span),
        non_tool_time_fraction=_ratio(max(0.0, span - tool_time), span),
        n_toolcalls=len(run.toolcalls),
    )


def baseline_mb(samples: Sequence[Sample], quantile: float = BASELINE_QUANTILE) -> float:
    if not samples:
        return 0.0
    ordered = sorted(s.mem_mb for s in samples)
    return round(ordered[min(int(len(ordered) * quantile), len(ordered) - 1)], 3)


def memory_profile(run: Run) -> MemoryProfile:
    mems = [s.mem_mb for s in run.samples]
    profile = MemoryProfile(baseline_mb=baseline_mb(run.samples))

    if mems:
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
    total_time = sum(call.duration_s for calls in groups.values() for call in calls)
    stats = []
    for name, calls in groups.items():
        durations = [call.duration_s for call in calls]
        peaks = [call.peak_memory_mb for call in calls]
        stats.append(
            ToolTypeStats(
                tool_type=name,
                n_calls=len(calls),
                total_time_s=round(sum(durations), 3),
                time_share=_ratio(sum(durations), total_time),
                mean_duration_s=_mean(durations),
                mean_peak_mb=_mean(peaks),
                max_peak_mb=round(max(peaks), 3) if peaks else 0.0,
            )
        )
    stats.sort(key=lambda s: s.total_time_s, reverse=True)
    return stats


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
        while (
            end < len(sequence)
            and sequence[end].tool_type == current.tool_type
            and sequence[end].command == current.command
        ):
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
        metrics.cpu_memory_correlation = _correlation(
            [s.mem_mb for s in run.samples], [s.cpu_pct for s in run.samples]
        )
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
    per_run = [analyze_run(run, burst_threshold_mb) for run in runs]
    all_calls = [call for run in runs for call in run.toolcalls]

    dataset = DatasetMetrics(
        n_runs=len(per_run),
        n_toolcalls=len(all_calls),
        runs=per_run,
        tool_types=tool_type_breakdown(all_calls),
        bash_categories=bash_category_breakdown(all_calls),
        degraded_runs=sum(1 for m in per_run if m.degraded),
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
