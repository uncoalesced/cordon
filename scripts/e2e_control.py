# Engineered by uncoalesced
"""End-to-end check of `cordon control run` on a real host (CI: ubuntu root/user, macOS, Windows).

Runs a child that allocates well past a small memory limit, then asserts the backend that was
chosen actually saw it. Exit 0 on pass, 1 with a reason on failure, 2 on harness errors.

    python scripts/e2e_control.py                        # expect the default backends for this OS
    python scripts/e2e_control.py --expect cgroup2       # e.g. ubuntu under sudo
    python scripts/e2e_control.py --expect cgroup2,systemd-run,advisory   # ubuntu as a user
    python scripts/e2e_control.py --expect null          # Windows

Defaults: linux -> cgroup2,systemd-run,advisory; darwin -> advisory; windows -> null.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

DEFAULT_EXPECT = {"linux": "cgroup2,systemd-run,advisory", "darwin": "advisory", "win32": "null"}

# Touch every page (bytes * n writes them all) so the memory is resident, then hold it.
CHILD = "import time; b = b'\\x01' * ({mb} << 20); time.sleep({hold})"


def _cordon() -> list[str]:
    found = shutil.which("cordon")
    return [found] if found else [sys.executable, "-m", "features.wrapper.cli"]


def _parse(stdout: str) -> dict:
    start = stdout.find("{")
    if start < 0:
        raise ValueError("no JSON object in cordon output")
    return json.loads(stdout[start:])


def check(result: dict, expected: set[str]) -> list[str]:
    problems: list[str] = []
    backend = result.get("backend")
    stats = result.get("stats", {})
    if backend not in expected:
        problems.append(f"backend {backend!r} not in expected {sorted(expected)}")
    if result.get("returncode") != 0:
        problems.append(f"child exited {result.get('returncode')} (error={result.get('error')!r})")
    if backend == "null":
        if result.get("attached") or result.get("feedback"):
            problems.append("null backend must not claim attachment or emit feedback")
        return problems
    if not result.get("attached"):
        problems.append(f"not attached (attach_error={stats.get('attach_error')!r})")
    if stats.get("high_events", 0) <= 0:
        problems.append(f"high_events={stats.get('high_events')} (expected > 0)")
    # The limit biting (high_events) is the enforcement proof. A warning is only owed once the
    # stall crosses the guard's own threshold; a throttle that barely hurt stays silent by design.
    from features.control.intent import FeedbackPolicy

    stall = float(stats.get("memory_stall_s") or 0.0)
    owed = stall >= FeedbackPolicy().threshold_s(float(result.get("duration_s") or 0.0))
    if owed and not result.get("feedback"):
        problems.append(f"stall {stall}s crossed the threshold but no feedback was emitted (source={stats.get('stall_source')!r})")
    if not owed and result.get("feedback") and not stats.get("oom_kills") and not stats.get("froze"):
        problems.append(f"feedback emitted for a {stall}s stall below the threshold")
    expected_source = "watchdog" if backend == "advisory" else "psi"
    if stats.get("stall_source") != expected_source:
        problems.append(f"stall_source={stats.get('stall_source')!r}, expected {expected_source!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expect", default=DEFAULT_EXPECT.get(sys.platform, "null"),
                        help="comma-separated acceptable backend names")  # fmt: skip
    parser.add_argument("--hint", default="memory:64M,cpu:low", help="AGENT_RESOURCE_HINT for the call")
    parser.add_argument("--mb", type=int, default=200, help="MB the child allocates")
    parser.add_argument("--hold", type=float, default=1.5, help="seconds the child holds the memory")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    expected = {name.strip() for name in args.expect.split(",") if name.strip()}

    cmd = [*_cordon(), "control", "run", "--json", "--hint", args.hint, "--",
           sys.executable, "-c", CHILD.format(mb=args.mb, hold=args.hold)]  # fmt: skip
    print("running:", " ".join(cmd), flush=True)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
        result = _parse(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"e2e_control: harness error: {exc}", file=sys.stderr)
        return 2

    stats = result.get("stats", {})
    print(
        f"backend={result.get('backend')} attached={result.get('attached')} rc={result.get('returncode')} "
        f"peak={stats.get('peak_memory_mb')}MB high_events={stats.get('high_events')} "
        f"stall={stats.get('memory_stall_s')}s source={stats.get('stall_source')!r} "
        f"attach_error={stats.get('attach_error')!r}"
    )
    print(f"feedback: {result.get('feedback') or '(none)'}")
    problems = check(result, expected)
    if problems:
        print("e2e_control: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        if done.stderr:
            print("--- cordon stderr ---\n" + done.stderr[-4000:], file=sys.stderr)
        return 1
    print(f"e2e_control: PASS ({result.get('backend')} backend)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
