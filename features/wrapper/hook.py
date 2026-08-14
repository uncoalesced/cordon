# Engineered by uncoalesced

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from features.adapters import get_adapter
from features.adapters.base import Adapter
from features.wrapper.logging_setup import configure, get_logger, log_failure
from features.wrapper.sampler import DEFAULT_INTERVAL_S, resolve_agent_root, stop_file
from features.wrapper.schema import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    RUN_LOG_FILENAME,
    JsonlWriter,
    Marker,
    call_key_for,
    summarize_command,
)

ENV_RUN_ROOT = "CORDON_RUN_ROOT"
ENV_INTERVAL = "CORDON_INTERVAL"
ENV_DISABLE = "CORDON_DISABLE"
ENV_TOOL = "CORDON_TOOL"

SAMPLER_PID_FILENAME = "sampler.pid"

_SPAWNING_EVENTS = {EVENT_SESSION_START, EVENT_TOOL_START}

_UNKNOWN_SESSION = "unknown-session"


def default_run_root() -> Path:
    override = os.environ.get(ENV_RUN_ROOT)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "runs"


def _interval() -> float:
    raw = os.environ.get(ENV_INTERVAL)
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        return float(raw)
    except ValueError:
        get_logger("hook").warning("bad %s=%r, using default", ENV_INTERVAL, raw)
        return DEFAULT_INTERVAL_S


def _process_start_time(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return time.time()


def sampler_pid_path(run_dir: Path) -> Path:
    return Path(run_dir) / SAMPLER_PID_FILENAME


def sampler_running(run_dir: Path) -> bool:
    path = sampler_pid_path(run_dir)
    if not path.exists():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        return psutil.Process(pid).is_running()
    except psutil.Error:
        return False


def spawn_sampler(run_dir: Path, agent_pid: int, interval: float) -> int | None:
    log = get_logger("hook")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if sampler_running(run_dir):
        return None

    stop_file(run_dir).unlink(missing_ok=True)

    command = [
        sys.executable,
        "-m",
        "features.wrapper.cli",
        "sample",
        "--run-dir",
        str(run_dir),
        "--pid",
        str(agent_pid),
        "--interval",
        str(interval),
    ]
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[2]),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, **kwargs)
    except OSError:
        log_failure(log, "sampler spawn failed", command=command, run_dir=str(run_dir))
        return None

    try:
        sampler_pid_path(run_dir).write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        log_failure(log, "could not record sampler pid", pid=proc.pid, run_dir=str(run_dir))

    log.info("sampler spawned | pid=%s agent_pid=%s run_dir=%s", proc.pid, agent_pid, run_dir)
    return proc.pid


def handle(
    payload: dict[str, Any],
    run_root: Path | None = None,
    now: float | None = None,
    adapter: Adapter | None = None,
) -> Marker | None:
    log = get_logger("hook")
    now = time.time() if now is None else now
    run_root = default_run_root() if run_root is None else Path(run_root)
    adapter = get_adapter(os.environ.get(ENV_TOOL)) if adapter is None else adapter

    normalized = adapter.normalize(payload)
    if not normalized.event:
        return None

    session_id = normalized.session_id or _UNKNOWN_SESSION
    run_dir = run_root / session_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_failure(log, "could not create run dir", run_dir=str(run_dir))
        return None
    configure(log_path=run_dir / RUN_LOG_FILENAME, to_stderr=False)

    agent_pid = resolve_agent_root()
    marker = Marker(
        event=normalized.event,
        ts=now,
        session_id=session_id,
        cwd=normalized.cwd,
        agent_pid=agent_pid,
        adapter=adapter.name,
        reported_duration_ms=normalized.reported_duration_ms,
    )

    if normalized.event in (EVENT_TOOL_START, EVENT_TOOL_END):
        marker.call_key = call_key_for(
            session_id, normalized.tool_name, normalized.tool_input, normalized.tool_use_id
        )
        marker.tool_type = normalized.tool_name
        marker.command = summarize_command(normalized.tool_name, normalized.tool_input)

    if normalized.event == EVENT_TOOL_END:
        marker.exit_status = _exit_status(normalized.tool_response)

    if normalized.event in _SPAWNING_EVENTS:
        spawn_sampler(run_dir, agent_pid, _interval())

    if normalized.event == EVENT_SESSION_END:
        stop_file(run_dir).touch()

    marker.hook_overhead_ms = round((time.time() - _process_start_time(os.getpid())) * 1000.0, 3)

    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(marker)
    return marker


def _exit_status(tool_response: Any) -> str:
    if isinstance(tool_response, dict):
        for key in ("exit_code", "exitCode", "status", "success"):
            if key in tool_response:
                return str(tool_response[key])
        if tool_response.get("is_error") or tool_response.get("isError"):
            return "error"
        if tool_response.get("interrupted"):
            return "interrupted"
        return "ok"
    if tool_response is None:
        return ""
    return "ok"


def main(argv: list[str] | None = None, tool: str | None = None) -> int:
    log = get_logger("hook")

    if os.environ.get(ENV_DISABLE):
        return 0

    try:
        adapter = get_adapter(tool or os.environ.get(ENV_TOOL))
    except KeyError:
        log_failure(log, "unknown tool, falling back to the default adapter", tool=tool)
        adapter = get_adapter()

    raw = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        log_failure(log, "could not read hook payload from stdin", raw=raw[:500])
        return 0

    try:
        handle(payload, adapter=adapter)
    except Exception:
        log_failure(
            log,
            "hook handling failed, agent unaffected",
            tool=adapter.name,
            event=payload.get("hook_event_name"),
            session_id=payload.get("session_id"),
            tool_name=payload.get("tool_name"),
        )

    return 0
