# Engineered by uncoalesced

from __future__ import annotations

import os
import signal
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from features import host
from features.wrapper.logging_setup import get_logger, log_failure
from features.wrapper.schema import (
    DEFAULT_INTERVAL_S,
    MARKERS_FILENAME,
    SAMPLES_FILENAME,
    JsonlWriter,
    Sample,
)

# psutil is imported inside the functions that need it: the hook imports this module on every
# tool call, and the hook path is latency-sensitive (see D7 in the plan).

STOP_FILENAME = "STOP"
AGENT_PID_FILENAME = "agent.pid"

ENV_AGENT_PID = "CORDON_AGENT_PID"
ENV_UNIQUE_EVERY = "CORDON_UNIQUE_EVERY"
ENV_IDLE_STOP = "CORDON_IDLE_STOP_S"
DEFAULT_UNIQUE_EVERY = 4
DEFAULT_IDLE_STOP_S = 1800.0

# Executable stems (".exe" stripped) of agents that ship as their own binary.
AGENT_EXECUTABLES = ("claude", "codex", "gemini", "cursor-agent", "hermes", "aider")
# Runtimes an agent may be hosted in; on their own they are only a weak hint.
INTERPRETERS = ("node", "bun", "deno", "python")
# Substrings of an interpreter's argv that identify the agent script it is running.
AGENT_ARGV_MARKERS = (
    "claude-code", "@anthropic-ai", "@openai/codex", "codex", "@google/gemini-cli", "gemini-cli",
    "cursor-agent", "hermes", "aider",
)
# Kept for callers that matched on plain names before scoring existed.
AGENT_PROCESS_NAMES = {n for stem in (*AGENT_EXECUTABLES, "node", "bun") for n in (stem, f"{stem}.exe")}

SCORE_ARGV = 3
SCORE_EXECUTABLE = 2
SCORE_INTERPRETER = 1

_LINUX_COMM_LEN = 15
_BYTES_PER_MB = 1024.0 * 1024.0


def _stem(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _is_agent_executable(stem: str) -> bool:
    if stem in AGENT_EXECUTABLES:
        return True
    # Linux truncates /proc/<pid>/comm to 15 chars, so a long name only matches as a prefix.
    return len(stem) == _LINUX_COMM_LEN and any(a.startswith(stem) for a in AGENT_EXECUTABLES)


def _is_interpreter(stem: str) -> bool:
    return stem in INTERPRETERS or stem.startswith("python")


def agent_score(name: str, cmdline: list[str]) -> int:
    """How strongly a process looks like the agent: argv-confirmed > agent binary > runtime."""
    stems = {_stem(name)}
    if cmdline:
        # A native claude install runs as a version-named file behind a `claude` symlink, so the
        # process name is "2.0.14" while argv[0] still says claude.
        stems.add(_stem(cmdline[0]))
    if any(_is_interpreter(s) for s in stems):
        args = " ".join(cmdline[1:]).replace("\\", "/").lower()
        if any(marker in args for marker in AGENT_ARGV_MARKERS):
            return SCORE_ARGV
    if any(_is_agent_executable(s) for s in stems):
        return SCORE_EXECUTABLE
    if any(_is_interpreter(s) for s in stems):
        return SCORE_INTERPRETER
    return 0


def _default_lookup(pid: int) -> Any:
    import psutil

    return psutil.Process(pid)


def resolve_agent_root(
    start_pid: int | None = None,
    max_depth: int = 12,
    lookup: Callable[[int], Any] | None = None,
) -> int:
    import psutil

    log = get_logger("sampler")
    override = os.environ.get(ENV_AGENT_PID, "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            log.warning("bad %s=%r, resolving by ancestry", ENV_AGENT_PID, override)

    lookup = _default_lookup if lookup is None else lookup
    start_pid = os.getpid() if start_pid is None else start_pid

    try:
        current = lookup(start_pid)
    except psutil.Error:
        log_failure(log, "cannot open start process, sampling it by pid anyway", start_pid=start_pid)
        return start_pid

    best_pid, best_score, fallback = start_pid, 0, start_pid
    for _ in range(max_depth):
        try:
            parent = current.parent()
        except psutil.Error:
            break
        if parent is None:
            break
        fallback = parent.pid
        try:
            name = parent.name()
        except psutil.Error:
            name = ""
        try:
            cmdline = list(parent.cmdline())
        except psutil.Error:
            cmdline = []
        score = agent_score(name, cmdline)
        # Strictly greater: on a tie the nearest ancestor wins.
        if score > best_score:
            best_pid, best_score = parent.pid, score
            if score == SCORE_ARGV:
                break
        current = parent

    if best_score:
        log.info("resolved agent root | pid=%s score=%s", best_pid, best_score)
        return best_pid
    log.warning("no agent-like ancestor, falling back to ancestor | pid=%s", fallback)
    return fallback


def agent_pid_path(run_dir: Path) -> Path:
    return Path(run_dir) / AGENT_PID_FILENAME


def process_identity(pid: int) -> float | None:
    """create_time of a live pid, or None. (pid, create_time) survives pid recycling."""
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def read_pid_file(path: Path) -> tuple[int, float | None] | None:
    try:
        parts = Path(path).read_text(encoding="utf-8").split()
        return int(parts[0]), (float(parts[1]) if len(parts) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None


def write_pid_file(path: Path, pid: int) -> None:
    created = process_identity(pid)
    Path(path).write_text(f"{pid} {created}" if created is not None else str(pid), encoding="utf-8")


def identity_matches(pid: int, created: float | None) -> bool:
    actual = process_identity(pid)
    if actual is None:
        return False
    return created is None or abs(actual - created) < 0.5


def cached_agent_root(run_dir: Path, validate: bool = True) -> int:
    """Agent pid for this run: cached in agent.pid after the first hook so later hooks skip the walk.

    validate=False trusts the cache without importing psutil (~60 ms on Windows). Safe for
    PostToolUse/SessionEnd: they follow a validated SessionStart/PreToolUse of the same live agent.
    """
    path = agent_pid_path(run_dir)
    cached = read_pid_file(path)
    if cached is not None and cached[1] is not None and (not validate or identity_matches(*cached)):
        return cached[0]
    pid = resolve_agent_root()
    try:
        write_pid_file(path, pid)
    except OSError:
        log_failure(get_logger("sampler"), "could not cache agent pid", path=str(path))
    return pid


def parse_pss_kb(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("Pss:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def unique_bytes(proc: Any, os_kind: str = host.OS, proc_root: str = "/proc") -> int:
    """Memory this process does not share: PSS on Linux, USS on macOS, private bytes on Windows."""
    if os_kind == host.LINUX:
        # smaps_rollup is one small read (kernel 4.14+); memory_full_info() parses full smaps.
        try:
            pss = parse_pss_kb(Path(proc_root, str(proc.pid), "smaps_rollup").read_text())
        except OSError:
            pss = None
        if pss is not None:
            return pss * 1024
    elif os_kind == host.DARWIN:
        import psutil

        try:
            return proc.memory_full_info().uss
        except psutil.AccessDenied:
            pass  # other-user/hardened processes: rss, so one denied pid cannot freeze the sum
    info = proc.memory_info()
    return getattr(info, "private", info.rss)


def exclusive_cgroup(
    root_pid: int, tree_pids: set[int], proc_root: str = "/proc", cg_root: str = "/sys/fs/cgroup"
) -> Path | None:
    """memory.current of the root's cgroup v2, if that cgroup holds nothing outside the tree."""
    try:
        lines = Path(proc_root, str(root_pid), "cgroup").read_text().splitlines()
        rel = next(line[3:] for line in lines if line.startswith("0::"))
        cg_dir = Path(cg_root) / rel.lstrip("/")
        members = {int(p) for p in (cg_dir / "cgroup.procs").read_text().split()}
    except (OSError, ValueError, StopIteration):
        return None
    current = cg_dir / "memory.current"
    if members and members <= tree_pids and current.exists():
        return current
    return None


class TreeSampler:
    def __init__(
        self,
        root_pid: int,
        interval: float = DEFAULT_INTERVAL_S,
        writer: JsonlWriter | None = None,
        unique_every: int | None = None,
    ) -> None:
        self.root_pid = root_pid
        self.interval = max(0.01, float(interval))
        self.writer = writer
        self.unique_every = max(1, unique_every or _env_int(ENV_UNIQUE_EVERY, DEFAULT_UNIQUE_EVERY))
        self.log = get_logger("sampler")
        self.partial_samples = 0
        self.failed_samples = 0
        self.errors: Counter[str] = Counter()
        self.tick_s_total = 0.0
        self.ticks = 0
        self._procs: dict[int, Any] = {}
        self._last_unique_mb: float | None = None
        self._cgroup: Path | None = None
        self._cgroup_checked = False

    def _tree(self) -> list[Any]:
        import psutil

        # ponytail: children(recursive=True) rescans the full process table each tick.
        # Costs a few ms at 250ms intervals; swap for an exec-event feed if it ever matters.
        try:
            root = psutil.Process(self.root_pid)
        except psutil.Error as exc:
            self.errors[type(exc).__name__] += 1
            return []
        try:
            return [root, *root.children(recursive=True)]
        except psutil.Error as exc:
            self.errors[type(exc).__name__] += 1
            return [root]

    def _tracked(self, proc: Any) -> Any:
        import psutil

        known = self._procs.get(proc.pid)
        if known is not None and known.pid == proc.pid:
            return known
        try:
            proc.cpu_percent(None)
        except psutil.Error:
            pass
        self._procs[proc.pid] = proc
        return proc

    def _cgroup_mb(self, tree: list[Any]) -> float | None:
        if host.OS != host.LINUX:
            return None
        if not self._cgroup_checked:
            # Checked once: a cgroup shared with unrelated processes would inflate every sample.
            self._cgroup_checked = True
            self._cgroup = exclusive_cgroup(self.root_pid, {p.pid for p in tree})
            self.log.info("cgroup memory source | path=%s", self._cgroup)
        if self._cgroup is None:
            return None
        try:
            return round(int(self._cgroup.read_text().strip()) / _BYTES_PER_MB, 3)
        except (OSError, ValueError):
            self._cgroup = None
            return None

    def sample_once(self) -> Sample:
        import psutil

        started = time.perf_counter()
        now = time.time()
        tree = self._tree()
        live_pids = {proc.pid for proc in tree}
        for pid in list(self._procs):
            if pid not in live_pids:
                del self._procs[pid]

        want_unique = self.ticks % self.unique_every == 0
        mem_bytes = 0
        unique = 0
        unique_ok = want_unique
        cpu_pct = 0.0
        counted = 0
        partial = False

        for proc in tree:
            tracked = self._tracked(proc)
            try:
                mem_bytes += tracked.memory_info().rss
                cpu_pct += tracked.cpu_percent(None)
                counted += 1
            except psutil.Error as exc:
                self.errors[type(exc).__name__] += 1
                partial = True
                continue
            if want_unique:
                try:
                    unique += unique_bytes(tracked)
                except psutil.Error as exc:
                    self.errors[type(exc).__name__] += 1
                    unique_ok = False

        if want_unique:
            # A partial unique sum is worse than the last good one; carry that instead.
            self._last_unique_mb = round(unique / _BYTES_PER_MB, 3) if unique_ok and counted else self._last_unique_mb

        if partial:
            self.partial_samples += 1

        sample = Sample(
            t=now,
            mem_mb=round(mem_bytes / _BYTES_PER_MB, 3),
            cpu_pct=round(cpu_pct, 2),
            n_procs=counted,
            partial=partial,
            mem_mb_unique=self._last_unique_mb,
            cg_mem_mb=self._cgroup_mb(tree) if tree else None,
        )
        self.ticks += 1
        self.tick_s_total += time.perf_counter() - started
        return sample

    def root_alive(self) -> bool:
        import psutil

        try:
            return psutil.Process(self.root_pid).is_running()
        except psutil.Error:
            return False

    def run(
        self,
        stop_check: Callable[[], bool] | None = None,
        max_samples: int | None = None,
        max_duration_s: float | None = None,
    ) -> int:
        self.log.info(
            "sampler starting | root_pid=%s interval=%.3fs unique_every=%s max_samples=%s max_duration=%s",
            self.root_pid,
            self.interval,
            self.unique_every,
            max_samples,
            max_duration_s,
        )

        started = time.monotonic()
        written = 0
        tick = 0

        try:
            while True:
                if stop_check is not None and stop_check():
                    self.log.info("sampler stopping: stop condition met")
                    break
                if max_samples is not None and written >= max_samples:
                    break
                if max_duration_s is not None and (time.monotonic() - started) >= max_duration_s:
                    break
                if not self.root_alive():
                    self.log.info("sampler stopping: root pid %s exited", self.root_pid)
                    break

                try:
                    sample = self.sample_once()
                    if self.writer is None or self.writer.write(sample):
                        written += 1
                except Exception:
                    self.failed_samples += 1
                    log_failure(
                        self.log,
                        "sample tick failed, continuing",
                        root_pid=self.root_pid,
                        tick=tick,
                        tracked_pids=sorted(self._procs),
                    )

                tick += 1
                deadline = started + tick * self.interval
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            self.log.info("sampler stopping: interrupted")

        self.log.info(
            "sampler stopped | written=%s partial=%s failed=%s mean_tick_ms=%.2f errors=%s",
            written,
            self.partial_samples,
            self.failed_samples,
            (self.tick_s_total / self.ticks * 1000.0) if self.ticks else 0.0,
            dict(self.errors),
        )
        return written


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def stop_file(run_dir: Path) -> Path:
    return Path(run_dir) / STOP_FILENAME


def idle_for(run_dir: Path, since: float, now: float | None = None) -> float:
    """Seconds since the last marker was written (or since `since` if none has been)."""
    now = time.time() if now is None else now
    try:
        last = (Path(run_dir) / MARKERS_FILENAME).stat().st_mtime
    except OSError:
        last = since
    return now - max(last, since)


def run_sampler(
    run_dir: Path,
    root_pid: int,
    interval: float = DEFAULT_INTERVAL_S,
    max_duration_s: float | None = None,
) -> int:
    run_dir = Path(run_dir)
    marker = stop_file(run_dir)
    idle_limit = env_float(ENV_IDLE_STOP, DEFAULT_IDLE_STOP_S)
    started = time.time()
    signalled: list[int] = []
    log = get_logger("sampler")

    if host.OS != host.WINDOWS:
        def _on_signal(signum: int, _frame: Any) -> None:
            signalled.append(signum)

        for name in ("SIGTERM", "SIGHUP"):
            try:
                signal.signal(getattr(signal, name), _on_signal)
            except (AttributeError, ValueError, OSError):
                pass

    last_idle_check = [0.0]

    def should_stop() -> bool:
        if signalled:
            log.info("sampler stopping: signal %s", signalled[0])
            return True
        if marker.exists():
            return True
        now = time.monotonic()
        if now - last_idle_check[0] >= 5.0:
            last_idle_check[0] = now
            # Catches a SessionEnd that never fired (agent killed, hook disabled mid-run).
            if idle_for(run_dir, started) > idle_limit:
                log.info("sampler stopping: no markers for over %.0fs", idle_limit)
                return True
        return False

    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        sampler = TreeSampler(root_pid=root_pid, interval=interval, writer=writer)
        return sampler.run(stop_check=should_stop, max_duration_s=max_duration_s)
