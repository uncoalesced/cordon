# Engineered by uncoalesced

from __future__ import annotations

import pytest

from features.control.cgroup import NullBackend
from features.control.contention import (
    ContentionResult,
    PhaseResult,
    WorkerResult,
    measure_solo,
    render,
    run_contention,
    run_phase,
)

TINY_WORK = 20_000


def _phase(name: str, pairs: list[tuple[str, float]]) -> PhaseResult:
    return PhaseResult(phase=name, workers=[WorkerResult(tier=t, elapsed_s=e, returncode=0) for t, e in pairs])


def test_solo_calibration_returns_a_positive_time():
    assert measure_solo(TINY_WORK) > 0.0


def test_a_phase_runs_every_worker_and_labels_its_tier():
    phase = run_phase(["high", "low", "low"], TINY_WORK, NullBackend(), guarded=False)
    assert len(phase.workers) == 3
    assert phase.survived() == 3
    assert [w.tier for w in phase.workers] == ["high", "low", "low"]
    assert all(w.elapsed_s > 0 for w in phase.workers)


def test_a_guarded_phase_creates_and_destroys_one_cgroup_per_worker():
    class Counting(NullBackend):
        def __init__(self) -> None:
            super().__init__()
            self.created: list[str] = []
            self.destroyed: list[str] = []

        def create(self, name: str):
            self.created.append(name)
            return super().create(name)

        def destroy(self, handle) -> bool:
            self.destroyed.append(handle.name)
            return True

    backend = Counting()
    run_phase(["high", "low"], TINY_WORK, backend, guarded=True)
    assert len(backend.created) == 2
    assert backend.destroyed == backend.created
    assert len(set(backend.created)) == 2


def test_an_unguarded_phase_creates_no_cgroups():
    class Forbidden(NullBackend):
        def create(self, name: str):
            raise AssertionError("baseline phase must not touch the backend")

    assert run_phase(["low"], TINY_WORK, Forbidden(), guarded=False).survived() == 1


def test_a_backend_that_fails_setup_still_runs_the_worker():
    class Exploding(NullBackend):
        def create(self, name: str):
            raise RuntimeError("cgroup create blew up")

    phase = run_phase(["high"], TINY_WORK, Exploding(), guarded=True)
    assert phase.survived() == 1
    assert phase.workers[0].cgroup_name == ""


def test_elapsed_lookup_filters_by_tier_and_success():
    phase = PhaseResult(
        phase="baseline",
        workers=[
            WorkerResult("high", 1.0, 0),
            WorkerResult("low", 5.0, 0),
            WorkerResult("low", 9.0, 1),
        ],
    )
    assert phase.elapsed_for("low") == [5.0]
    assert phase.survived() == 2


def test_the_experiment_runs_both_arms_end_to_end():
    result = run_contention(high=1, low=2, work=TINY_WORK, backend=NullBackend())
    assert len(result.baseline.workers) == 3
    assert len(result.guarded.workers) == 3
    assert result.backend == "null"


def test_the_report_names_the_headline_comparison():
    result = ContentionResult(
        solo_s=1.0,
        work=TINY_WORK,
        cpu_count=8,
        backend="cgroup2",
        baseline=_phase("baseline", [("high", 4.0), ("low", 4.0), ("low", 4.2)]),
        guarded=_phase("guarded", [("high", 1.4), ("low", 6.0), ("low", 6.4)]),
    )
    report = render(result)
    assert "HIGH p95 completion: 4.000s -> 1.400s (-65.0%)" in report
    assert "survival: baseline 3/3, guarded 3/3" in report


def test_the_report_refuses_to_claim_a_result_on_the_null_backend():
    result = ContentionResult(
        solo_s=1.0,
        work=TINY_WORK,
        cpu_count=8,
        backend="null",
        baseline=_phase("baseline", [("high", 4.0)]),
        guarded=_phase("guarded", [("high", 1.0)]),
    )
    assert "not enforcement" in render(result)


@pytest.mark.integration
def test_real_contention_produces_comparable_arms():
    result = run_contention(high=1, low=2, work=200_000, backend=NullBackend())
    assert result.baseline.survived() == 3
    assert result.guarded.survived() == 3
    assert result.solo_s > 0
