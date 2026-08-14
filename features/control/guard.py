# Engineered by uncoalesced

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from features.control.cgroup import CgroupStats, call_cgroup_name, select_backend
from features.control.intent import FeedbackPolicy, Intent, resolve_intent
from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.sampler import DEFAULT_INTERVAL_S
from features.wrapper.schema import append_jsonl

CONTROL_FILENAME = "control.jsonl"


@dataclass
class GuardResult:
    argv: list[str]
    returncode: int
    start_ts: float
    end_ts: float
    duration_s: float
    cgroup_name: str
    backend: str
    attached: bool
    intent: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    feedback: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_guarded(
    argv: Sequence[str],
    hint: str | None = None,
    backend: Any = None,
    policy: FeedbackPolicy | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    interval: float = DEFAULT_INTERVAL_S,
    timeout: float | None = None,
    record_path: Path | None = None,
    stderr_passthrough: bool = True,
) -> GuardResult:
    log = get_logger("guard")
    argv = list(argv)
    env = dict(os.environ if env is None else env)
    backend = select_backend() if backend is None else backend
    policy = FeedbackPolicy() if policy is None else policy

    intent = resolve_intent(hint, env=env)
    if hint:
        env.setdefault("AGENT_RESOURCE_HINT", hint)

    name = call_cgroup_name()
    start = time.time()
    handle = None
    attached = False
    stats = CgroupStats()
    returncode = -1
    error = ""

    try:
        handle = backend.create(name)
        backend.apply(handle, intent)
    except Exception:
        log_failure(log, "cgroup setup failed, running unguarded", name=name, argv=argv)
        handle = None

    stderr_file = tempfile.TemporaryFile(mode="w+b") if stderr_passthrough else None

    try:
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": env,
            "stderr": stderr_file if stderr_file is not None else None,
        }
        if handle is not None and os.name == "posix":
            popen_kwargs["preexec_fn"] = lambda: backend.join_self(handle)

        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
        log_failure(log, "guarded command failed to start", argv=argv, cgroup=name)
        if handle is not None:
            backend.destroy(handle)
        if stderr_file is not None:
            stderr_file.close()
        end = time.time()
        return GuardResult(
            argv=argv,
            returncode=127,
            start_ts=start,
            end_ts=end,
            duration_s=round(end - start, 4),
            cgroup_name=name,
            backend=getattr(backend, "name", "unknown"),
            attached=False,
            intent=intent.to_dict(),
            stats=stats.to_dict(),
            error=error,
        )

    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if handle is not None:
                if not attached:
                    attached = _confirm(backend, handle, log)
                stats = _snapshot(backend, handle, stats, log)
            if deadline is not None and time.monotonic() >= deadline:
                log.warning("guarded command exceeded timeout, killing | argv=%s timeout=%s", argv, timeout)
                error = "timeout"
                proc.kill()
                break
            time.sleep(interval)
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        returncode = proc.wait()
        error = "interrupted"

    if handle is not None:
        stats = _snapshot(backend, handle, stats, log)

    end = time.time()
    duration = round(end - start, 4)

    feedback = ""
    try:
        feedback = policy.evaluate(
            command=" ".join(argv),
            intent=intent,
            stall_s=stats.memory_stall_s,
            duration_s=duration,
            peak_memory_mb=stats.peak_memory_mb,
            froze=stats.froze,
            oom_kills=stats.oom_kills,
            observable=stats.observable and attached,
        ) or ""
    except Exception:
        log_failure(log, "feedback evaluation failed, suppressing message", cgroup=name, argv=argv)

    if stderr_file is not None:
        _emit_stderr(stderr_file, feedback, log)
    elif feedback:
        _write_stderr(feedback + "\n", log)

    if handle is not None:
        try:
            backend.destroy(handle)
        except Exception:
            log_failure(log, "cgroup teardown failed", cgroup=name)

    result = GuardResult(
        argv=argv,
        returncode=returncode,
        start_ts=start,
        end_ts=end,
        duration_s=duration,
        cgroup_name=name,
        backend=getattr(backend, "name", "unknown"),
        attached=attached,
        intent=intent.to_dict(),
        stats=stats.to_dict(),
        feedback=feedback,
        error=error,
    )

    if record_path is not None:
        append_jsonl(Path(record_path), result)

    log.info(
        "guarded call finished | cgroup=%s rc=%s duration=%.3fs peak=%.1fMB stall=%.3fs feedback=%s",
        name,
        returncode,
        duration,
        stats.peak_memory_mb,
        stats.memory_stall_s,
        bool(feedback),
    )
    return result


def _confirm(backend: Any, handle: Any, log: Any) -> bool:
    try:
        return bool(backend.confirm_membership(handle))
    except Exception:
        log_failure(log, "membership check failed", cgroup=getattr(handle, "name", ""))
        return False


def _snapshot(backend: Any, handle: Any, previous: CgroupStats, log: Any) -> CgroupStats:
    try:
        return backend.read_stats(handle)
    except Exception:
        log_failure(log, "cgroup stat read failed, keeping last snapshot", cgroup=getattr(handle, "name", ""))
        return previous


def _emit_stderr(stderr_file: Any, feedback: str, log: Any) -> None:
    try:
        stderr_file.seek(0)
        captured = stderr_file.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        log_failure(log, "could not read captured stderr")
        captured = ""
    finally:
        try:
            stderr_file.close()
        except OSError:
            pass

    payload = captured
    if feedback:
        if payload and not payload.endswith("\n"):
            payload += "\n"
        payload += feedback + "\n"
    if payload:
        _write_stderr(payload, log)


def _write_stderr(payload: str, log: Any) -> None:
    try:
        sys.stderr.write(payload)
        sys.stderr.flush()
    except (OSError, ValueError):
        log_failure(log, "could not write to stderr", payload=payload[:200])
