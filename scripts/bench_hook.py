# Engineered by uncoalesced
"""Measure `cordon hook` wall time the way an agent pays for it: one fresh process per event.

Each iteration fires a PreToolUse then a PostToolUse, so both hook paths are timed.
Usage: python scripts/bench_hook.py [--n 20] [--max-median-ms 80] [--cordon PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def _payload(event: str) -> bytes:
    body = {
        "hook_event_name": event,
        "session_id": "bench",
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "tool_use_id": "t1",
    }
    if event == "PostToolUse":
        body["tool_response"] = {"exit_code": 0}
    return json.dumps(body).encode()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--max-median-ms", type=float, default=None, help="exit 1 when the median is above this")
    ap.add_argument("--cordon", default=shutil.which("cordon") or "cordon")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="cordon-bench-", ignore_cleanup_errors=True) as root:
        env = dict(os.environ, CORDON_RUN_ROOT=root)
        env.pop("CORDON_DISABLE", None)

        def fire(event: str) -> float:
            t0 = time.perf_counter()
            subprocess.run([args.cordon, "hook"], input=_payload(event), env=env, check=True, capture_output=True)
            return (time.perf_counter() - t0) * 1000

        fire("PreToolUse")  # warm-up: spawns the sampler, fills the disk cache
        fire("PostToolUse")
        pre, post = [], []
        for _ in range(args.n):
            pre.append(fire("PreToolUse"))
            post.append(fire("PostToolUse"))
        fire("SessionEnd")

    both = pre + post
    median = statistics.median(both)
    print(
        f"median={median:.1f} pre={statistics.median(pre):.1f} post={statistics.median(post):.1f} "
        f"min={min(both):.1f} max={max(both):.1f} ms (n={args.n} pairs)"
    )
    if args.max_median_ms is not None and median > args.max_median_ms:
        print(f"FAIL: median above {args.max_median_ms} ms", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
