# Engineered by uncoalesced

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from features.control.cgroup import call_cgroup_name, select_backend
from features.control.intent import resolve_intent
from features.wrapper.logging_setup import get_logger, log_failure

DEFAULT_WORK = 12_000_000
POLL_S = 0.02

ENV_PROCS = "CORDON_CGROUP_PROCS"

WORKER_SOURCE = (
    "import os,sys,time\n"
    f"p=os.environ.get({ENV_PROCS!r})\n"
    "if p:\n"
    "    try:\n"
    "        open(p,'w').write(str(os.getpid()))\n"
    "    except OSError:\n"
    "        pass\n"
    "n=int(sys.argv[1]);t=time.perf_counter();x=0\n"
    "while x<n:\n"
    "    x+=1\n"
    "sys.stdout.write('%.4f'%(time.perf_counter()-t))\n"
)


@dataclass
class WorkerResult:
    tier: str
    elapsed_s: float
    returncode: int
    cgroup_name: str = ""


@dataclass
class PhaseResult:
    phase: str
    workers: list[WorkerResult] = field(default_factory=list)

    def survived(self) -> int:
        return sum(1 for w in self.workers if w.returncode == 0)

    def elapsed_for(self, tier: str) -> list[float]:
        return sorted(w.elapsed_s for w in self.workers if w.tier == tier and w.returncode == 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContentionResult:
    solo_s: float
    work: int
    cpu_count: int
    backend: str
    baseline: PhaseResult
    guarded: PhaseResult

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
    return values[index]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _spawn(work: int, procs_path: str | None) -> subprocess.Popen:
    env = dict(os.environ)
    if procs_path:
        env[ENV_PROCS] = procs_path
    else:
        env.pop(ENV_PROCS, None)
    return subprocess.Popen(
        [sys.executable, "-c", WORKER_SOURCE, str(work)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _collect(proc: subprocess.Popen, tier: str, cgroup_name: str, fallback_s: float) -> WorkerResult:
    try:
        out, _ = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    try:
        elapsed = float((out or b"").decode("utf-8", errors="replace").strip())
    except ValueError:
        elapsed = fallback_s
    return WorkerResult(tier=tier, elapsed_s=elapsed, returncode=proc.returncode, cgroup_name=cgroup_name)


def measure_solo(work: int = DEFAULT_WORK) -> float:
    start = time.perf_counter()
    proc = _spawn(work, None)
    result = _collect(proc, "solo", "", time.perf_counter() - start)
    return result.elapsed_s


def run_phase(
    tiers: list[str],
    work: int,
    backend: Any,
    guarded: bool,
) -> PhaseResult:
    log = get_logger("contention")
    phase = PhaseResult(phase="guarded" if guarded else "baseline")
    handles: list[Any] = []
    launched: list[tuple[subprocess.Popen, str, str]] = []
    started = time.perf_counter()

    try:
        for index, tier in enumerate(tiers):
            procs_path = None
            cgroup_name = ""
            if guarded:
                try:
                    handle = backend.create(call_cgroup_name(ts=time.time() + index))
                    backend.apply(handle, resolve_intent(f"cpu:{tier},memory:max"))
                    handles.append(handle)
                    cgroup_name = handle.name
                    procs_path = str(handle.path / "cgroup.procs") if handle.path else None
                except Exception:
                    log_failure(log, "cgroup setup failed for worker, running it unguarded", tier=tier, index=index)
            launched.append((_spawn(work, procs_path), tier, cgroup_name))

        while any(proc.poll() is None for proc, _, _ in launched):
            time.sleep(POLL_S)

        fallback = time.perf_counter() - started
        phase.workers = [_collect(proc, tier, name, fallback) for proc, tier, name in launched]
    finally:
        for handle in handles:
            try:
                backend.destroy(handle)
            except Exception:
                log_failure(log, "cgroup teardown failed", cgroup=getattr(handle, "name", ""))

    log.info(
        "phase finished | phase=%s workers=%s survived=%s",
        phase.phase,
        len(phase.workers),
        phase.survived(),
    )
    return phase


def run_contention(
    high: int = 1,
    low: int | None = None,
    work: int = DEFAULT_WORK,
    backend: Any = None,
) -> ContentionResult:
    backend = select_backend() if backend is None else backend
    cpu_count = os.cpu_count() or 4
    low = min(8, max(3, cpu_count)) if low is None else low
    tiers = ["high"] * high + ["low"] * low

    solo = measure_solo(work)
    baseline = run_phase(tiers, work, backend, guarded=False)
    guarded = run_phase(tiers, work, backend, guarded=True)

    return ContentionResult(
        solo_s=solo,
        work=work,
        cpu_count=cpu_count,
        backend=getattr(backend, "name", "unknown"),
        baseline=baseline,
        guarded=guarded,
    )


def render(result: ContentionResult) -> str:
    lines = [
        "# Synthetic CPU contention",
        "",
        f"backend={result.backend}  cpus={result.cpu_count}  work={result.work}  "
        f"solo={result.solo_s:.3f}s",
        f"workers: {len(result.baseline.workers)} "
        f"({sum(1 for w in result.baseline.workers if w.tier == 'high')} high, "
        f"{sum(1 for w in result.baseline.workers if w.tier == 'low')} low)",
        "",
        "| tier | phase | n | mean s | p95 s | max s |",
        "|---|---|---|---|---|---|",
    ]

    for tier in ("high", "low"):
        for phase in (result.baseline, result.guarded):
            values = phase.elapsed_for(tier)
            if not values:
                continue
            lines.append(
                f"| {tier} | {phase.phase} | {len(values)} | {_mean(values):.3f} | "
                f"{_percentile(values, 0.95):.3f} | {max(values):.3f} |"
            )

    base_high = result.baseline.elapsed_for("high")
    guard_high = result.guarded.elapsed_for("high")
    lines.append("")
    if base_high and guard_high:
        before = _percentile(base_high, 0.95)
        after = _percentile(guard_high, 0.95)
        change = ((after - before) / before * 100.0) if before else 0.0
        lines.append(f"HIGH p95 completion: {before:.3f}s -> {after:.3f}s ({change:+.1f}%)")
    lines.append(
        f"survival: baseline {result.baseline.survived()}/{len(result.baseline.workers)}, "
        f"guarded {result.guarded.survived()}/{len(result.guarded.workers)}"
    )
    if result.backend == "null":
        lines.append("")
        lines.append(
            "Backend is null: no cgroup existed in either phase, so the two arms are the same\n"
            "experiment run twice. Any difference here is noise, not enforcement."
        )
    return "\n".join(lines)
