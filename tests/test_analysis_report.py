# Engineered by uncoalesced

from __future__ import annotations

from features.analysis.metrics import analyze_dataset
from features.analysis.report import comparison_rows, render_report


def _verdicts(dataset):
    return {row.metric: row.verdict for row in comparison_rows(dataset)}


def test_empty_dataset_reports_no_data_and_keeps_methodology():
    report = render_report(analyze_dataset([]))
    assert "## No dataset yet" in report
    assert "## Methodology" in report
    assert "## Known divergences from the paper's setup" in report


def test_empty_dataset_verdicts_are_all_no_data():
    assert set(_verdicts(analyze_dataset([])).values()) == {"no data"}


def test_matching_run_is_reported_as_matching(make_run, make_call, make_samples):
    calls = [
        make_call(command="pytest -q", start_ts=0.0, duration=6.0, peak=900.0, avg=300.0),
        make_call(command="git status", tool_type="Bash", start_ts=7.0, duration=4.0),
        make_call(command="a.py", tool_type="Read", start_ts=12.0, duration=4.0),
    ]
    samples = make_samples([(float(t) / 2, 200.0 if t % 4 else 900.0) for t in range(24)])
    dataset = analyze_dataset([make_run(calls=calls, samples=samples)])

    verdicts = _verdicts(dataset)

    assert verdicts["Bash share of tool time"] == "matches"
    assert verdicts["Test execution share of Bash time"] == "matches"


def test_retry_prevalence_lands_in_range_across_a_batch(make_run, make_call, make_samples):
    samples = make_samples([(0.0, 200), (5.0, 200)])
    looping = [
        make_run(
            task_id=f"loop-{i}",
            calls=[make_call(command="pytest", start_ts=float(j)) for j in range(3)],
            samples=samples,
        )
        for i in range(9)
    ]
    clean = [make_run(task_id="clean", calls=[make_call(command="ls")], samples=samples)]

    assert _verdicts(analyze_dataset(looping + clean))["Runs containing retry groups"] == "matches"


def test_divergence_is_named_with_a_direction(make_run, make_call, make_samples):
    dataset = analyze_dataset(
        [make_run(calls=[make_call(command="ls", duration=1.0)], samples=make_samples([(0.0, 200), (100.0, 200)]))]
    )

    verdicts = _verdicts(dataset)

    assert verdicts["Tool execution share of session time"] == "diverges (lower)"
    assert verdicts["Runs containing retry groups"] == "diverges (lower)"
    assert verdicts["Test execution share of Bash time"] == "diverges (lower)"


def test_report_renders_every_section(make_run, make_call, make_samples):
    calls = [make_call(command="pytest", start_ts=float(i), duration=1.0) for i in range(3)]
    dataset = analyze_dataset([make_run(calls=calls, samples=make_samples([(0.0, 200), (1.0, 900), (5.0, 200)]))])

    report = render_report(dataset)

    for heading in (
        "## Comparison against the paper",
        "## Per-tool-type breakdown",
        "## Bash command categories",
        "## Per-run detail",
        "## Retry loops",
        "## Methodology",
    ):
        assert heading in report
    assert "| Bash |" in report
    assert "| test |" in report
    assert "pytest" in report


def test_report_flags_degraded_runs(make_run, monkeypatch):
    monkeypatch.setattr(
        "features.analysis.metrics.execution_split",
        lambda _run: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = render_report(analyze_dataset([make_run()]))
    assert "analyzed in degraded mode" in report


def test_report_honours_a_custom_title():
    assert render_report(analyze_dataset([]), title="Cordon Run 7").startswith("# Cordon Run 7")


def test_report_records_run_and_call_counts(make_run, make_call, make_samples):
    dataset = analyze_dataset([make_run(calls=[make_call()], samples=make_samples([(0.0, 200), (2.0, 200)]))])
    assert "from 1 run(s) and 1 tool call(s)" in render_report(dataset)
