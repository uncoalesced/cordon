# Engineered by uncoalesced

from __future__ import annotations

import functools
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from features import host
from features.control.cgroup import CgroupStats, attach_in_child, call_cgroup_name, select_backend
from features.control.intent import FeedbackPolicy, resolve_intent
from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.schema import DEFAULT_INTERVAL_S, append_jsonl

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

    attach_error = ""
    exec_argv = argv
    try:
        handle = backend.create(name)
        name = handle.name
        backend.apply(handle, intent)
        wrap = getattr(backend, "wrap_argv", None)
        exec_argv = list(wrap(handle, argv)) if wrap else argv
    except Exception:
        log_failure(log, "cgroup setup failed, running unguarded", name=name, argv=argv)
        if handle is not None:
            try:
                backend.destroy(handle)  # created but not applied: do not leak an empty cgroup
            except Exception:
                log_failure(log, "cgroup teardown after failed setup also failed", cgroup=name)
        handle = None
        exec_argv = argv

    stderr_file = tempfile.TemporaryFile(mode="w+b") if stderr_passthrough else None
    report_r = report_w = None

    try:
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": env,
            "stderr": stderr_file if stderr_file is not None else None,
        }
        if handle is not None and host.OS != host.WINDOWS:
            # The child cannot log between fork and exec; it reports a failed join as b"E<errno>"
            # on this pipe instead. preexec_fn runs before close_fds, so the fd is still open.
            report_r, report_w = os.pipe()
            popen_kwargs["preexec_fn"] = functools.partial(attach_in_child, backend, handle, report_w)

        try:
            proc = subprocess.Popen(exec_argv, **popen_kwargs)
        finally:
            if report_w is not None:
                os.close(report_w)
        if report_r is not None:
            attach_error = _read_attach_report(report_r, log, name)
    except OSError as exc:
        if report_r is not None:
            os.close(report_r)
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

    if handle is not None:
        try:
            bind = getattr(backend, "bind_pid", None)
            if bind:
                bind(handle, proc.pid)
        except Exception:
            log_failure(log, "backend could not bind the child pid", cgroup=name, pid=proc.pid)

    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if handle is not None:
                if not attached and not attach_error:
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
        if not attached and not attach_error:
            attached = _confirm(backend, handle, log)
        stats = _snapshot(backend, handle, stats, log)
    stats.attach_error = attach_error

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
            stall_source=stats.stall_source or "psi",
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


def _read_attach_report(fd: int, log: Any, name: str) -> str:
    # Popen returns only after exec (or its failure), so the child has written or exited by now.
    try:
        data = os.read(fd, 64)
    except OSError:
        data = b""
    finally:
        os.close(fd)
    if not data.startswith(b"E"):
        return ""
    try:
        code = int(data[1:] or b"0")
    except ValueError:
        code = 0
    reason = f"join failed: errno {code} ({os.strerror(code)})" if code else "join failed"
    log.warning("child could not join its cgroup, running unattached | cgroup=%s %s", name, reason)
    return reason


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
