# Engineered by uncoalesced

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from features.analysis.metrics import DatasetMetrics, RunMetrics, ToolTypeStats

PAPER = "AgentCgroup §6"

METHODOLOGY = """## Methodology

Cordon samples the agent's whole process tree (RSS summed, CPU percent summed) at a fixed
interval for the duration of a session, while `PreToolUse` / `PostToolUse` hooks write
timestamped markers. `cordon reduce` slices the sample stream by tool-call window; the numbers
below come from those slices plus the uncut session stream.

Definitions used here, stated because they are choices rather than givens:

- **Baseline memory** — median of samples falling outside every tool-call window. This is the
  framework's resting footprint, the layer AgentCgroup §6 measures at ~185MB.
- **Burst** — a sample exceeding baseline by more than the burst threshold (300MB by default,
  matching the paper's ">300MB" bursts).
- **Tool time** — the union of tool-call windows, not their sum, so overlapping concurrent
  calls cannot push the fraction above 1.
- **Retry group** — three or more strictly consecutive tool calls of the same type with a
  byte-identical command, matching the paper's definition.
- **Task peak/avg ratio** — session peak memory over session mean memory, the shape of the
  paper's 15.4× headline figure.

## Known divergences from the paper's setup

These are structural, known before any data was collected, and they are why some rows below
cannot match even in principle:

1. **No container.** The paper runs Claude Code inside Podman and attributes 29–45% of task
   time to container initialization. Cordon measures a natively-installed agent, so the
   initialization phase does not exist here. The paper's combined "56–74% OS overhead" figure
   is therefore not reproducible; only its tool-execution half is.
2. **Different process shape.** The paper's baseline is a single containerized Node runtime at
   ~185MB. The measured target here is a desktop Claude Code install spanning ~10–11 processes,
   with a resting tree footprint several times larger. Ratios computed against that larger
   baseline are compressed relative to the paper's, and absolute megabyte comparisons are not
   meaningful — the *shape* of the burst behaviour is what carries over.
3. **RSS double-counts shared pages** across a parent and its children. The paper reports RSS
   too, so the bias is shared, but it grows with process count and this tree is wider.
4. **Sampling is 250ms, not the paper's continuous accounting.** Tool calls shorter than one
   tick land in no sample at all; those are counted as empty windows by the reducer rather
   than silently reported as zero-cost.
"""


@dataclass
class Comparison:
    metric: str
    measured: str
    paper: str
    verdict: str


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}"


def _verdict(value: float | None, low: float, high: float) -> str:
    if value is None:
        return "no data"
    if value < low:
        return "diverges (lower)"
    if value > high:
        return "diverges (higher)"
    return "matches"


def _share_of(stats: Sequence[ToolTypeStats], name: str) -> float | None:
    if not stats:
        return None
    for entry in stats:
        if entry.tool_type == name:
            return entry.time_share
    return 0.0


def _when(condition: bool, value: float | None) -> float | None:
    return value if condition else None


def comparison_rows(dataset: DatasetMetrics) -> list[Comparison]:
    has_runs = dataset.n_runs > 0

    bash_share = _share_of(dataset.tool_types, "Bash")
    test_share = _share_of(dataset.bash_categories, "test")
    peak_ratio = _when(dataset.max_task_peak_avg_ratio > 0, dataset.max_task_peak_avg_ratio)
    retry_prevalence = _when(has_runs, dataset.runs_with_retries_fraction)
    retry_time = _when(has_runs, dataset.mean_retry_time_fraction)
    tool_fraction = _when(has_runs, dataset.mean_tool_time_fraction)
    sampled_in_tools = _when(has_runs, dataset.mean_sample_time_in_tools_fraction)
    bursts_in_tools = _when(
        any(run.bursts.n_burst_samples for run in dataset.runs), dataset.mean_bursts_in_tools_fraction
    )
    change_rate = _when(dataset.max_change_rate_mb_per_s > 0, dataset.max_change_rate_mb_per_s)

    return [
        Comparison(
            "Tool execution share of session time",
            _pct(tool_fraction),
            "36.4–42.5% of active time",
            _verdict(tool_fraction, 0.364, 0.425),
        ),
        Comparison(
            "Task peak/avg memory ratio (max)",
            _num(peak_ratio, "×"),
            "15.4× (pydicom outlier)",
            _verdict(peak_ratio, 2.0, 15.4),
        ),
        Comparison(
            "Bash share of tool time",
            _pct(bash_share),
            "47.8–98.1%",
            _verdict(bash_share, 0.478, 0.981),
        ),
        Comparison(
            "Test execution share of Bash time",
            _pct(test_share),
            "43.7–72.9%",
            _verdict(test_share, 0.437, 0.729),
        ),
        Comparison(
            "Runs containing retry groups",
            _pct(retry_prevalence),
            "85–97% of tasks",
            _verdict(retry_prevalence, 0.85, 0.97),
        ),
        Comparison(
            "Time spent inside retry groups",
            _pct(retry_time),
            "7.4–20.5%",
            _verdict(retry_time, 0.074, 0.205),
        ),
        Comparison(
            "CPU/memory correlation range",
            f"{_num(dataset.correlation_min)} to {_num(dataset.correlation_max)}",
            "-0.84 to +0.50 (mean -0.39)",
            _verdict(dataset.correlation_mean, -0.84, 0.50),
        ),
        Comparison(
            "Sampling time inside tool calls",
            _pct(sampled_in_tools),
            "35.9–50.6%",
            _verdict(sampled_in_tools, 0.359, 0.506),
        ),
        Comparison(
            "Bursts above threshold inside tool calls",
            _pct(bursts_in_tools),
            "67.3–98.5%",
            _verdict(bursts_in_tools, 0.673, 0.985),
        ),
        Comparison(
            "Peak memory change rate",
            _num(change_rate, " MB/s"),
            "~3000 MB/s",
            _verdict(change_rate, 1000.0, 1e9),
        ),
    ]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "_No data._\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def _tool_table(stats: Sequence[ToolTypeStats], label: str) -> str:
    return _table(
        [label, "Calls", "Total time (s)", "Share of time", "Mean duration (s)", "Mean peak (MB)", "Max peak (MB)"],
        [
            [
                entry.tool_type,
                str(entry.n_calls),
                f"{entry.total_time_s:.1f}",
                _pct(entry.time_share),
                f"{entry.mean_duration_s:.2f}",
                f"{entry.mean_peak_mb:.1f}",
                f"{entry.max_peak_mb:.1f}",
            ]
            for entry in stats
        ],
    )


def _run_table(runs: Sequence[RunMetrics]) -> str:
    return _table(
        ["Task", "Calls", "Span (s)", "Tool time", "Baseline (MB)", "Peak (MB)", "Peak/avg", "Retry groups", "CPU/mem r"],
        [
            [
                run.task_id[:24],
                str(run.execution.n_toolcalls),
                f"{run.execution.span_s:.1f}",
                _pct(run.execution.tool_time_fraction),
                f"{run.memory.baseline_mb:.1f}",
                f"{run.memory.peak_mb:.1f}",
                _num(run.memory.task_peak_avg_ratio, "×"),
                str(run.retries.n_groups),
                _num(run.cpu_memory_correlation),
            ]
            for run in runs
        ],
    )


def render_report(dataset: DatasetMetrics, title: str = "Stage 1 — Characterization Findings") -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = [
        f"# {title}",
        "",
        "<!-- Generated by `cordon analyze`. Hand edits below the generated tables are lost on regeneration. -->",
        "",
        f"Generated {generated} from {dataset.n_runs} run(s) and {dataset.n_toolcalls} tool call(s), "
        f"compared against {PAPER}.",
        "",
    ]

    if dataset.n_runs == 0:
        sections += [
            "## No dataset yet",
            "",
            "No reduced runs were found. Install the hooks into a task repo, run the agent across a",
            "batch of tasks, reduce each session, then re-run `cordon analyze` to replace this section",
            "with measured results.",
            "",
            METHODOLOGY,
        ]
        return "\n".join(sections)

    if dataset.degraded_runs:
        sections += [
            f"> **{dataset.degraded_runs} of {dataset.n_runs} runs analyzed in degraded mode.** "
            "Their metrics are partial; see the run log for tracebacks.",
            "",
        ]

    sections += [
        "## Comparison against the paper",
        "",
        _table(
            ["Metric", "Measured", f"{PAPER}", "Verdict"],
            [[row.metric, row.measured, row.paper, row.verdict] for row in comparison_rows(dataset)],
        ),
        "",
        "## Per-tool-type breakdown",
        "",
        _tool_table(dataset.tool_types, "Tool"),
        "",
        "## Bash command categories",
        "",
        _tool_table(dataset.bash_categories, "Category"),
        "",
        "## Per-run detail",
        "",
        _run_table(dataset.runs),
        "",
        "## Retry loops",
        "",
    ]

    retry_rows = [
        [run.task_id[:24], group.command[:60], str(group.length), f"{group.total_time_s:.1f}"]
        for run in dataset.runs
        for group in run.retries.groups
    ]
    sections += [
        _table(["Task", "Command", "Repeats", "Time (s)"], retry_rows),
        "",
        METHODOLOGY,
    ]

    return "\n".join(sections)
