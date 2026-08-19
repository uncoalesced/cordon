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

SAMPLER_PID_FILENAME = "sampler.pid"

# Claude Code, Codex, Hermes, Cursor, and Gemini CLI all fire the same four lifecycle moments
# through a hook; each just spells the event name differently. See features/wrapper/agents.py
# for the config-side half of this (where each agent's hook file lives, what shape it wants).
_START_EVENTS = {"SessionStart", "on_session_start", "sessionStart"}
_END_EVENTS = {"SessionEnd", "Stop", "on_session_end", "sessionEnd", "stop"}
_PRE_EVENTS = {"PreToolUse", "pre_tool_call", "preToolUse", "BeforeTool"}
_POST_EVENTS = {"PostToolUse", "post_tool_call", "postToolUse", "postToolUseFailure", "AfterTool"}

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


def handle(payload: dict[str, Any], run_root: Path | None = None, now: float | None = None) -> Marker | None:
    log = get_logger("hook")
    now = time.time() if now is None else now
    run_root = default_run_root() if run_root is None else Path(run_root)

    event_name = str(payload.get("hook_event_name", ""))
    # Cursor's tool hooks key session identity as "conversation_id" instead of "session_id"
    # (its own sessionStart/sessionEnd events use "session_id", so both are checked here).
    session_id = str(payload.get("session_id") or payload.get("conversation_id") or _UNKNOWN_SESSION)
    run_dir = run_root / session_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_failure(log, "could not create run dir", run_dir=str(run_dir))
        return None
    configure(log_path=run_dir / RUN_LOG_FILENAME, to_stderr=False)

    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {})
    # Hermes nests the tool-call id in "extra" instead of sending it top-level.
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    tool_use_id = str(payload.get("tool_use_id") or extra.get("tool_call_id") or "")
    agent_pid = resolve_agent_root()

    if event_name in _START_EVENTS:
        marker = Marker(
            event=EVENT_SESSION_START,
            ts=now,
            session_id=session_id,
            cwd=str(payload.get("cwd", "")),
            agent_pid=agent_pid,
        )
        spawn_sampler(run_dir, agent_pid, _interval())
    elif event_name in _PRE_EVENTS:
        marker = Marker(
            event=EVENT_TOOL_START,
            ts=now,
            session_id=session_id,
            call_key=call_key_for(session_id, tool_name, tool_input, tool_use_id),
            tool_type=tool_name,
            command=summarize_command(tool_name, tool_input),
            cwd=str(payload.get("cwd", "")),
            agent_pid=agent_pid,
        )
        spawn_sampler(run_dir, agent_pid, _interval())
    elif event_name in _POST_EVENTS:
        marker = Marker(
            event=EVENT_TOOL_END,
            ts=now,
            session_id=session_id,
            call_key=call_key_for(session_id, tool_name, tool_input, tool_use_id),
            tool_type=tool_name,
            command=summarize_command(tool_name, tool_input),
            cwd=str(payload.get("cwd", "")),
            exit_status=_exit_status(_response_source(payload, extra)),
            agent_pid=agent_pid,
        )
    elif event_name in _END_EVENTS:
        marker = Marker(
            event=EVENT_SESSION_END,
            ts=now,
            session_id=session_id,
            agent_pid=agent_pid,
        )
        stop_file(run_dir).touch()
    else:
        log.warning("ignoring unrecognised hook event | event=%r session=%s", event_name, session_id)
        return None

    marker.hook_overhead_ms = round((time.time() - _process_start_time(os.getpid())) * 1000.0, 3)

    with JsonlWriter(run_dir / MARKERS_FILENAME) as writer:
        writer.write(marker)
    return marker


def _response_source(payload: dict[str, Any], extra: dict[str, Any]) -> Any:
    # Where the post-call result lives differs per agent: Claude Code/Codex/Gemini send a
    # "tool_response" object; Cursor sends "tool_output" as a JSON *string*, or (on failure)
    # skips both in favour of top-level "error_message"/"failure_type"; Hermes reports outcome
    # inside "extra" (status/error_type/duration_ms) rather than as its own top-level field.
    if "tool_response" in payload:
        return payload.get("tool_response")
    if "tool_output" in payload:
        return payload.get("tool_output")
    if "failure_type" in payload or "error_message" in payload:
        return {"status": "error", "error_type": payload.get("failure_type"), "error_message": payload.get("error_message")}
    if "status" in extra or "error_type" in extra:
        return extra
    return None


def _exit_status(response: Any) -> str:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return "ok" if response else ""
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "status", "success"):
            if key in response:
                return str(response[key])
        if response.get("is_error") or response.get("isError") or response.get("error") or response.get("error_type"):
            return "error"
        return "ok"
    if response is None:
        return ""
    return "ok"


def main(argv: list[str] | None = None) -> int:
    log = get_logger("hook")

    if os.environ.get(ENV_DISABLE):
        return 0

    raw = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        log_failure(log, "could not read hook payload from stdin", raw=raw[:500])
        return 0

    try:
        handle(payload)
    except Exception:
        log_failure(
            log,
            "hook handling failed, agent unaffected",
            event=payload.get("hook_event_name"),
            session_id=payload.get("session_id"),
            tool_name=payload.get("tool_name"),
        )

    return 0
