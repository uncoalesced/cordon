# Engineered by uncoalesced

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from features import host
from features.wrapper import agents
from features.wrapper.schema import MARKERS_FILENAME, SAMPLES_FILENAME

# `cordon doctor`: every link between "agent fires a hook" and "report.md exists", exercised for
# real against a throwaway run root. Each check is independent and never raises; a crash inside
# one is reported as that check's FAIL so the rest still run.

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

ROUNDTRIP_TIMEOUT_S = 30.0
SAMPLER_WAIT_S = 5.0
DOCTOR_SESSION = "cordon-doctor"


@dataclass
class CheckResult:
    check: str
    status: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def check_binary() -> CheckResult:
    script = host.venv_script("cordon")
    if host.is_executable(script):
        return CheckResult("binary", PASS, str(script))
    why = "not executable" if script.exists() else "missing"
    fix = f"chmod +x {host.shell_quote(str(script))}" if script.exists() else "pip install -e . (in the cordon venv)"
    return CheckResult("binary", FAIL, f"{script} {why}; hooks fall back to `python -m`", fix)


def _commands_in(node: Any) -> set[str]:
    """Every "command" string anywhere in a parsed settings document (all agents' shapes)."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                found.add(value)
            else:
                found |= _commands_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _commands_in(item)
    return found


def check_installed(agent: str, scope: str, target: str, command: str) -> CheckResult:
    from features.wrapper.cli import install_hooks_hint

    path = agents.settings_path(agent, agents.install_target(scope, target).resolve())
    fix = install_hooks_hint(agent, scope, target)
    if not path.exists():
        return CheckResult("installed", WARN, f"{path} does not exist", fix)
    try:
        text = path.read_text(encoding="utf-8")
        if agent == agents.HERMES:
            import yaml

            document = yaml.safe_load(text)
        else:
            document = json.loads(text)
    except Exception as exc:
        return CheckResult("installed", FAIL, f"{path} unreadable: {exc}", f"fix the syntax in {path}")
    commands = _commands_in(document)
    if command in commands:
        return CheckResult("installed", PASS, f"{agent} hook present in {path}")
    stale = sorted(c for c in commands if "cordon" in c and " hook" in c)
    detail = f"{path} has a different cordon hook ({stale[0]})" if stale else f"no cordon hook in {path}"
    return CheckResult("installed", WARN, detail, fix)


def _synthetic_payload() -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": DOCTOR_SESSION,
        "tool_name": "Bash",
        "tool_input": {"command": "cordon doctor"},
        "tool_response": {"exit_code": 0},
        "cwd": os.getcwd(),
    }


def check_roundtrip(command: str, run_root: Path) -> CheckResult:
    # shell=True is the agent's own launcher: /bin/sh -c on POSIX, cmd.exe /c on Windows.
    env = dict(os.environ)
    env["CORDON_RUN_ROOT"] = str(run_root)
    env.pop("CORDON_DISABLE", None)
    # The doctor's own process stands in for the agent; skips the ancestry walk.
    env["CORDON_AGENT_PID"] = str(os.getpid())
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(_synthetic_payload()).encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=ROUNDTRIP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return CheckResult("roundtrip", FAIL, f"hook command hung for {ROUNDTRIP_TIMEOUT_S:.0f}s: {command}", "run it by hand and read its stderr")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    markers = run_root / DOCTOR_SESSION / MARKERS_FILENAME
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = err[-1] if err else "no stderr"
        return CheckResult("roundtrip", FAIL, f"exit {proc.returncode} ({tail})", f"run by hand: {command}")
    if not markers.exists() or not markers.read_text(encoding="utf-8").strip():
        return CheckResult("roundtrip", FAIL, f"exit 0 but no {MARKERS_FILENAME} written", f"check {run_root / DOCTOR_SESSION} / cordon.log")
    return CheckResult("roundtrip", PASS, f"exit 0, marker written, {elapsed_ms:.0f} ms")


def check_agent_root() -> CheckResult:
    import psutil

    from features.wrapper.sampler import agent_score, resolve_agent_root

    saved = os.environ.pop("CORDON_AGENT_PID", None)
    try:
        pid = resolve_agent_root()
    finally:
        if saved is not None:
            os.environ["CORDON_AGENT_PID"] = saved
    try:
        proc = psutil.Process(pid)
        name, cmdline = proc.name(), proc.cmdline()
    except psutil.Error:
        name, cmdline = "?", []
    score = agent_score(name, cmdline)
    if score:
        return CheckResult("agent_root", PASS, f"pid {pid} ({name}, score {score})")
    return CheckResult(
        "agent_root",
        WARN,
        f"no agent-like ancestor; fell back to pid {pid} ({name})",
        "expected outside an agent; inside one, set CORDON_AGENT_PID",
    )


def check_sampler(run_dir: Path) -> CheckResult:
    from features.wrapper.finalize import wait_for_sampler
    from features.wrapper.hook import spawn_sampler
    from features.wrapper.sampler import stop_file

    pid = spawn_sampler(run_dir, os.getpid(), 0.1)
    if pid is None:
        return CheckResult("sampler", FAIL, "spawn failed", f"read {run_dir / 'sampler.stderr'}")
    samples = run_dir / SAMPLES_FILENAME
    deadline = time.monotonic() + SAMPLER_WAIT_S
    while time.monotonic() < deadline and not (samples.exists() and samples.stat().st_size):
        time.sleep(0.05)
    got_samples = samples.exists() and samples.stat().st_size > 0
    stop_file(run_dir).touch()
    stopped = wait_for_sampler(run_dir, SAMPLER_WAIT_S)
    if not got_samples:
        return CheckResult("sampler", FAIL, f"pid {pid} wrote no samples in {SAMPLER_WAIT_S:.0f}s", f"read {run_dir / 'sampler.stderr'}")
    if not stopped:
        return CheckResult("sampler", FAIL, f"pid {pid} ignored STOP for {SAMPLER_WAIT_S:.0f}s", f"kill {pid}; read cordon.log")
    return CheckResult("sampler", PASS, f"pid {pid} sampled and stopped on STOP")


def check_finalize(run_dir: Path) -> CheckResult:
    from features.wrapper.finalize import finalize

    out = finalize(run_dir, wait_s=1.0)
    if out is None or not Path(out).exists():
        return CheckResult("finalize", FAIL, "no report.md produced", f"read {run_dir / 'cordon.log'}")
    return CheckResult("finalize", PASS, "report.md written")


def check_run_root(root: Path) -> CheckResult:
    from features.wrapper.finalize import collect_status

    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".doctor-"):
            pass
    except OSError as exc:
        return CheckResult("run_root", FAIL, f"{root} not writable: {exc}", "set CORDON_RUN_ROOT to a writable dir")
    rows = collect_status(root)
    live = sum(row.sampler_live for row in rows)
    status = WARN if live else PASS
    fix = "cordon status --clean" if live else ""
    return CheckResult("run_root", status, f"{root}: {len(rows)} runs, {live} live samplers", fix)


def check_enforcement() -> CheckResult:
    from features.control import probe

    tier = probe.enforcement_tier(probe.probe())
    if host.OS == host.WINDOWS:
        return CheckResult("enforcement", PASS, f"tier {tier}: Windows is measurement only")
    return CheckResult("enforcement", PASS, f"tier {tier} (see `cordon control probe`)")


def _guarded(name: str, check: Callable[[], CheckResult]) -> CheckResult:
    try:
        return check()
    except Exception as exc:
        return CheckResult(name, FAIL, f"check crashed: {type(exc).__name__}: {exc}", "re-run with -v and read the traceback")


def run_checks(agent: str = agents.CLAUDE_CODE, scope: str = agents.SCOPE_PROJECT, target: str = ".") -> list[CheckResult]:
    from features.wrapper.cli import _hook_command, install_hook_command, user_run_root
    from features.wrapper.hook import default_run_root

    user_global = agents.is_user_global(agent, scope)
    installed = install_hook_command(agent, scope)
    real_root = user_run_root() if user_global else default_run_root()

    scratch = Path(tempfile.mkdtemp(prefix="cordon-doctor-"))
    try:
        # Same construction as the installed command; a --run-root is swapped for the scratch dir
        # so the round trip never writes into the user's real runs.
        probe_command = _hook_command(scratch) if user_global else installed
        run_dir = scratch / DOCTOR_SESSION
        results = [
            _guarded("binary", check_binary),
            _guarded("installed", lambda: check_installed(agent, scope, target, installed)),
            _guarded("roundtrip", lambda: check_roundtrip(probe_command, scratch)),
            _guarded("agent_root", check_agent_root),
        ]
        run_dir.mkdir(parents=True, exist_ok=True)
        results += [
            _guarded("sampler", lambda: check_sampler(run_dir)),
            _guarded("finalize", lambda: check_finalize(run_dir)),
            _guarded("run_root", lambda: check_run_root(real_root)),
            _guarded("enforcement", check_enforcement),
        ]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


def render(results: list[CheckResult]) -> str:
    width = max(len(r.check) for r in results)
    lines = []
    for r in results:
        lines.append(f"{r.status}  {r.check.ljust(width)}  {r.detail}")
        if r.fix and r.status != PASS:
            lines.append(f"      {' ' * width}  fix: {r.fix}")
    failed = sum(r.status == FAIL for r in results)
    warned = sum(r.status == WARN for r in results)
    lines.append("")
    lines.append(f"{failed} failed, {warned} warnings" if failed or warned else "all checks passed")
    return "\n".join(lines)
