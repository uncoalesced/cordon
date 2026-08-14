# Engineered by uncoalesced

from __future__ import annotations

import pytest

from features.analysis.metrics import (
    analyze_dataset,
    analyze_run,
    bash_category_breakdown,
    baseline_mb,
    burst_profile,
    classify_bash,
    execution_split,
    in_any_window,
    memory_profile,
    retry_profile,
    tool_type_breakdown,
    union_seconds,
)


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest tests/ -q", "test"),
        ("python -m pytest", "test"),
        ("cargo test --all", "test"),
        ("npm run test", "test"),
        ("pip install -e .", "install"),
        ("uv pip install psutil", "install"),
        ("npm ci", "install"),
        ("make -j8", "build"),
        ("cargo build --release", "build"),
        ("git status --short", "vcs"),
        ("gh pr list", "vcs"),
        ("python script.py", "python"),
        ("ls -la", "filesystem"),
        ("rg pattern src/", "filesystem"),
        ("./weird-binary --flag", "other"),
        ("", "other"),
    ],
)
def test_classify_bash(command, expected):
    assert classify_bash(command) == expected


def test_classify_bash_prefers_test_over_python():
    assert classify_bash("python -m pytest tests/") == "test"


def test_union_seconds_merges_overlaps():
    assert union_seconds([(0.0, 2.0), (1.0, 3.0)]) == 3.0
    assert union_seconds([(0.0, 1.0), (2.0, 3.0)]) == 2.0
    assert union_seconds([(0.0, 5.0), (1.0, 2.0)]) == 5.0
    assert union_seconds([]) == 0.0


def test_in_any_window_is_inclusive():
    windows = [(1.0, 2.0)]
    assert in_any_window(1.0, windows) and in_any_window(2.0, windows)
    assert not in_any_window(0.9, windows)


def test_execution_split_uses_the_window_union(make_run, make_call, make_samples):
    run = make_run(
        calls=[make_call(start_ts=1.0, duration=2.0), make_call(start_ts=2.0, duration=2.0, command="ls")],
        samples=make_samples([(0.0, 100), (10.0, 100)]),
    )
    split = execution_split(run)
    assert split.span_s == 10.0
    assert split.tool_time_s == 3.0
    assert split.tool_time_fraction == 0.3
    assert split.non_tool_time_fraction == 0.7
    assert split.n_toolcalls == 2


def test_execution_split_on_a_run_with_no_samples(make_run, make_call):
    split = execution_split(make_run(calls=[make_call()]))
    assert split.span_s == 0.0
    assert split.tool_time_fraction == 0.0


def test_baseline_is_a_low_quantile_of_the_stream(make_samples):
    samples = make_samples([(float(t), 100.0 + t) for t in range(20)])
    assert baseline_mb(samples) == 102.0


def test_baseline_ignores_bursts_even_when_they_dominate(make_samples):
    samples = make_samples([(0.0, 200), (1.0, 4000), (2.0, 3800), (3.0, 3900)])
    assert baseline_mb(samples) == 200.0


def test_baseline_of_nothing_is_zero():
    assert baseline_mb([]) == 0.0


def test_memory_profile_reports_task_and_call_ratios(make_run, make_call, make_samples):
    run = make_run(
        calls=[
            make_call(command="pytest", peak=900.0, avg=300.0),
            make_call(command="ls", start_ts=5.0, peak=200.0, avg=190.0),
        ],
        samples=make_samples([(0.0, 200), (1.0, 900), (2.0, 200), (3.0, 200)]),
    )
    profile = memory_profile(run)
    assert profile.peak_mb == 900.0
    assert profile.avg_mb == 375.0
    assert profile.task_peak_avg_ratio == 2.4
    assert profile.max_call_peak_avg_ratio == 3.0
    assert profile.max_ratio_command == "pytest"
    assert profile.call_peak_avg_ratios == [3.0, pytest.approx(1.0526, abs=1e-4)]


def test_memory_profile_ignores_calls_with_no_samples(make_run, make_call):
    profile = memory_profile(make_run(calls=[make_call(peak=0.0, avg=0.0)]))
    assert profile.call_peak_avg_ratios == []
    assert profile.max_ratio_command == ""


def test_burst_profile_concentrates_bursts_in_tool_windows(make_run, make_call, make_samples):
    run = make_run(
        calls=[make_call(start_ts=2.0, duration=2.0)],
        samples=make_samples([(0.0, 200), (1.0, 200), (2.0, 250), (3.0, 900), (4.0, 850), (5.0, 210)]),
    )
    profile = burst_profile(run)
    assert profile.baseline_mb == 200.0
    assert profile.n_burst_samples == 2
    assert profile.bursts_in_tools_fraction == 1.0
    assert profile.sample_time_in_tools_fraction == 0.5


def test_burst_profile_measures_change_rate_and_duration(make_run, make_samples):
    run = make_run(samples=make_samples([(0.0, 200), (0.25, 1400), (0.5, 1450), (0.75, 200)]))
    profile = burst_profile(run)
    assert profile.max_change_rate_mb_per_s == 5000.0
    assert profile.median_burst_duration_s == 0.25


def test_burst_profile_counts_a_burst_running_to_the_end(make_run, make_samples):
    profile = burst_profile(make_run(samples=make_samples([(0.0, 200), (1.0, 900), (2.0, 950)])))
    assert profile.n_burst_samples == 2
    assert profile.median_burst_duration_s == 1.0


def test_burst_profile_on_empty_samples(make_run):
    profile = burst_profile(make_run())
    assert profile.n_samples == 0
    assert profile.bursts_in_tools_fraction == 0.0


def test_tool_type_breakdown_shares_sum_to_one(make_call):
    calls = [
        make_call(tool_type="Bash", duration=6.0, peak=900.0),
        make_call(tool_type="Bash", duration=2.0, peak=300.0),
        make_call(tool_type="Read", duration=2.0, peak=200.0),
    ]
    stats = tool_type_breakdown(calls)
    assert [s.tool_type for s in stats] == ["Bash", "Read"]
    assert stats[0].time_share == 0.8
    assert stats[0].n_calls == 2
    assert stats[0].max_peak_mb == 900.0
    assert round(sum(s.time_share for s in stats), 4) == 1.0


def test_tool_type_breakdown_labels_missing_types(make_call):
    assert tool_type_breakdown([make_call(tool_type="")])[0].tool_type == "unknown"


def test_bash_category_breakdown_only_covers_bash(make_call):
    calls = [
        make_call(tool_type="Bash", command="pytest -q", duration=7.0),
        make_call(tool_type="Bash", command="git status", duration=3.0),
        make_call(tool_type="Read", command="a.py", duration=90.0),
    ]
    stats = bash_category_breakdown(calls)
    assert {s.tool_type for s in stats} == {"test", "vcs"}
    assert stats[0].tool_type == "test"
    assert stats[0].time_share == 0.7


def test_retry_profile_detects_consecutive_identical_calls(make_call):
    calls = [make_call(command="pytest", start_ts=float(i), duration=1.0) for i in range(4)]
    profile = retry_profile(calls)
    assert profile.n_groups == 1
    assert profile.longest_group == 4
    assert profile.retry_time_s == 4.0
    assert profile.retry_time_fraction == 1.0


def test_retry_profile_needs_three_in_a_row(make_call):
    calls = [make_call(command="pytest", start_ts=0.0), make_call(command="pytest", start_ts=1.0)]
    assert retry_profile(calls).n_groups == 0


def test_retry_profile_breaks_on_an_intervening_call(make_call):
    calls = [
        make_call(command="pytest", start_ts=0.0),
        make_call(command="pytest", start_ts=1.0),
        make_call(command="a.py", tool_type="Read", start_ts=2.0),
        make_call(command="pytest", start_ts=3.0),
    ]
    assert retry_profile(calls).n_groups == 0


def test_retry_profile_can_ignore_interleaved_tools(make_call):
    calls = [
        make_call(command="pytest", start_ts=0.0),
        make_call(command="pytest", start_ts=1.0),
        make_call(command="a.py", tool_type="Read", start_ts=2.0),
        make_call(command="pytest", start_ts=3.0),
    ]
    assert retry_profile(calls, ignore_tools=("Read",)).n_groups == 1


def test_retry_profile_only_watches_the_named_tools(make_call):
    calls = [make_call(command="a.py", tool_type="Read", start_ts=float(i)) for i in range(5)]
    assert retry_profile(calls).n_groups == 0


def test_retry_profile_finds_multiple_groups(make_call):
    calls = (
        [make_call(command="pytest", start_ts=float(i)) for i in range(3)]
        + [make_call(command="ls", start_ts=10.0 + i) for i in range(3)]
    )
    profile = retry_profile(calls)
    assert profile.n_groups == 2
    assert {g.command for g in profile.groups} == {"pytest", "ls"}


def test_analyze_run_computes_correlation(make_run, make_call, make_samples):
    samples = make_samples([(0.0, 100, 10.0), (1.0, 200, 20.0), (2.0, 300, 30.0)])
    metrics = analyze_run(make_run(calls=[make_call()], samples=samples))
    assert metrics.cpu_memory_correlation == 1.0
    assert metrics.degraded is False


def test_analyze_run_reports_negative_correlation(make_run, make_samples):
    samples = make_samples([(0.0, 100, 30.0), (1.0, 200, 20.0), (2.0, 300, 10.0)])
    assert analyze_run(make_run(samples=samples)).cpu_memory_correlation == -1.0


def test_analyze_run_returns_no_correlation_without_variance(make_run, make_samples):
    samples = make_samples([(0.0, 100, 5.0), (1.0, 100, 5.0)])
    assert analyze_run(make_run(samples=samples)).cpu_memory_correlation is None


def test_analyze_run_degrades_instead_of_raising(make_run, monkeypatch):
    monkeypatch.setattr(
        "features.analysis.metrics.execution_split",
        lambda _run: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    metrics = analyze_run(make_run())
    assert metrics.degraded is True
    assert metrics.execution.span_s == 0.0


def test_analyze_dataset_aggregates_across_runs(make_run, make_call, make_samples):
    quiet = make_run(
        task_id="quiet",
        calls=[make_call(command="git status", duration=1.0, peak=200.0, avg=195.0, start_ts=1.0)],
        samples=make_samples([(0.0, 200), (1.0, 205), (2.0, 200), (3.0, 200)]),
    )
    bursty = make_run(
        task_id="bursty",
        calls=[make_call(command="pytest", start_ts=1.0, duration=2.0, peak=1000.0, avg=400.0)],
        samples=make_samples([(0.0, 200), (1.0, 1000), (2.0, 800), (3.0, 200)]),
    )

    dataset = analyze_dataset([quiet, bursty])

    assert dataset.n_runs == 2
    assert dataset.n_toolcalls == 2
    assert dataset.outlier_task_id == "bursty"
    assert dataset.max_task_peak_avg_ratio > dataset.mean_task_peak_avg_ratio
    assert dataset.degraded_runs == 0
    assert [s.tool_type for s in dataset.bash_categories] == ["test", "vcs"]


def test_analyze_dataset_reports_retry_prevalence(make_run, make_call, make_samples):
    with_retries = make_run(
        task_id="loops",
        calls=[make_call(command="pytest", start_ts=float(i)) for i in range(3)],
        samples=make_samples([(0.0, 200), (5.0, 200)]),
    )
    without = make_run(task_id="clean", calls=[make_call(command="ls")], samples=make_samples([(0.0, 200), (5.0, 200)]))

    dataset = analyze_dataset([with_retries, without])

    assert dataset.runs_with_retries_fraction == 0.5
    assert dataset.mean_retry_time_fraction == 0.5


def test_analyze_dataset_on_no_runs_is_all_zeroes():
    dataset = analyze_dataset([])
    assert dataset.n_runs == 0
    assert dataset.correlation_mean is None
    assert dataset.outlier_task_id == ""
