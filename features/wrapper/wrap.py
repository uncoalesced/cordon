# Engineered by uncoalesced

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from features.wrapper.hook import default_run_root, spawn_sampler
from features.wrapper.logging_setup import configure, get_logger, log_failure
from features.wrapper.sampler import DEFAULT_INTERVAL_S, stop_file
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    MARKERS_FILENAME,
    RUN_LOG_FILENAME,
    JsonlWriter,
    Marker,
)

# For agents with no PreToolUse/PostToolUse-style hook system - Aider is the current example -
# there is nothing to intercept between "the agent decides to act" and "the action runs", so
# per-tool-call markers aren't possible. What's still possible, and still useful: wrap the whole
# agent invocation, sample its process tree for the run's duration, and get one session-level
# peak/avg memory and CPU record - the same shape `cordon reduce` already produces when a hook
# run has zero paired tool calls. Point this at the exact command that launches the agent.


@dataclass
class WrapResult:
    argv: list[str]
    session_id: str
    run_dir: str
    returncode: int
    start_ts: float
    end_ts: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_wrap(
    argv: Sequence[str],
    run_root: Path | None = None,
    interval: float | None = None,
    session_id: str | None = None,
) -> WrapResult:
    argv = list(argv)
    run_root = default_run_root() if run_root is None else Path(run_root)
    session_id = session_id or f"wrap-{int(time.time())}"
    run_dir = run_root / session_id
    configure(log_path=run_dir / RUN_LOG_FILENAME, to_stderr=False)
    log = get_logger("wrap")

    try:
        proc = subprocess.Popen(argv)
    except OSError:
        log_failure(log, "wrapped command failed to start", argv=argv, run_dir=str(run_dir))
        end = time.time()
        return WrapResult(argv=argv, session_id=session_id, run_dir=str(run_dir), returncode=127, start_ts=end, end_ts=end)

    start = time.time()
    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(Marker(event=EVENT_SESSION_START, ts=start, session_id=session_id, agent_pid=proc.pid))

    spawn_sampler(run_dir, proc.pid, interval or DEFAULT_INTERVAL_S)

    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        returncode = proc.wait()

    end = time.time()
    stop_file(run_dir).touch()
    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(Marker(event=EVENT_SESSION_END, ts=end, session_id=session_id, agent_pid=proc.pid))

    log.info("wrap finished | argv=%s pid=%s rc=%s run_dir=%s", argv, proc.pid, returncode, run_dir)
    return WrapResult(argv=argv, session_id=session_id, run_dir=str(run_dir), returncode=returncode, start_ts=start, end_ts=end)


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    command = raw[1:] if raw and raw[0] == "--" else raw
    if not command:
        sys.stderr.write("cordon wrap needs a command after --\n")
        return 2
    result = run_wrap(command)
    return result.returncode
