# Engineered by uncoalesced
"""`cordon serve`: one quiet per-user daemon that records every session.

Agents that can POST hook events (Claude Code's `"type": "http"` hooks) send them straight to
this server, so a tool call costs a loopback request instead of a new process, and no sampler or
finalize process is ever spawned per session: sampling runs as one thread per live session and
reports are written in-process. Being the only writer of each markers.jsonl also means two
concurrent hooks can no longer tear a line.

SessionStart stays a command hook (`cordon serve --ensure`): it is a child of the agent, so it
can tell the daemon which pid to sample, and it starts the daemon if it is not running.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The client half (health/post_hook) runs inside `serve --ensure`, which blocks session start:
# it speaks HTTP over a raw socket because urllib.request/http.client import ~85 ms of modules
# (email, tempfile, ...). Only the long-lived server imports http.server.

from cordon import __version__, host
from cordon.wrapper.logging_setup import configure, get_logger, log_failure

DEFAULT_PORT = 47821
ENV_PORT = "CORDON_PORT"
STATE_FILENAME = "daemon.json"
LOG_FILENAME = "daemon.log"
HOOK_PATH = "/hook"
HEALTH_PATH = "/health"
MAX_BODY = 4 << 20
# Exit once no recorded agent is alive for this long; the next SessionStart starts a fresh one.
DEFAULT_IDLE_EXIT_S = 3600.0
REAP_EVERY_S = 15.0
FINALIZE_JOIN_S = 5.0


def port() -> int:
    try:
        return int(os.environ.get(ENV_PORT, "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def base_url(port_: int | None = None) -> str:
    return f"http://127.0.0.1:{port() if port_ is None else port_}"


def hook_url(port_: int | None = None, run_root: Path | str | None = None) -> str:
    """The URL an http hook POSTs to. run_root rides in the query so one daemon serves every repo."""
    url = base_url(port_) + HOOK_PATH
    if run_root is not None:
        url += "?" + urlencode({"run_root": str(run_root)}, quote_via=quote)
    return url


def _request(port_: int | None, method: str, target: str, body: bytes = b"", timeout: float = 2.0) -> tuple[int, bytes]:
    """One HTTP/1.0 exchange with the daemon. Raises OSError/ValueError on any failure."""
    port_ = port() if port_ is None else port_
    head = f"{method} {target} HTTP/1.0\r\nHost: 127.0.0.1:{port_}\r\nContent-Length: {len(body)}\r\n"
    if body:
        head += "Content-Type: application/json\r\n"
    with socket.create_connection(("127.0.0.1", port_), timeout=timeout) as conn:
        conn.sendall(head.encode("ascii") + b"\r\n" + body)
        chunks = []
        while chunk := conn.recv(65536):
            chunks.append(chunk)
    raw = b"".join(chunks)
    status_line, _, rest = raw.partition(b"\r\n")
    _, _, payload = rest.partition(b"\r\n\r\n")
    return int(status_line.split()[1]), payload


def health(port_: int | None = None, timeout: float = 0.5) -> dict[str, Any] | None:
    try:
        status, body = _request(port_, "GET", HEALTH_PATH, timeout=timeout)
        info = json.loads(body or b"{}") if status == 200 else {}
    except (OSError, ValueError, IndexError):
        return None
    return info if info.get("service") == "cordon" else None


def post_hook(
    payload: dict[str, Any],
    port_: int | None = None,
    run_root: Path | str | None = None,
    agent_pid: int | None = None,
    timeout: float = 2.0,
) -> bool:
    target = HOOK_PATH
    query = {}
    if run_root is not None:
        query["run_root"] = str(run_root)
    if agent_pid:
        query["agent_pid"] = str(int(agent_pid))
    if query:
        target += "?" + urlencode(query, quote_via=quote)
    try:
        status, _ = _request(port_, "POST", target, json.dumps(payload).encode(), timeout=timeout)
    except (OSError, ValueError, IndexError):
        return False
    return 200 <= status < 300


@dataclass
class Session:
    run_dir: Path
    agent_pid: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    sampler: threading.Thread | None = None
    finalized: bool = False
    last_seen: float = field(default_factory=time.time)


class Daemon:
    def __init__(self, default_run_root: Path, interval: float | None = None, idle_exit_s: float = DEFAULT_IDLE_EXIT_S):
        from cordon.wrapper.schema import DEFAULT_INTERVAL_S

        self.default_run_root = Path(default_run_root)
        self.interval = DEFAULT_INTERVAL_S if interval is None else interval
        self.idle_exit_s = idle_exit_s
        self.sessions: dict[Path, Session] = {}
        self._lock = threading.Lock()
        self._last_live = time.time()
        self.log = get_logger("daemon")

    # -- hook events ---------------------------------------------------------------------------

    def _session(self, run_dir: Path) -> Session:
        with self._lock:
            session = self.sessions.get(run_dir)
            if session is None or session.finalized:
                session = self.sessions[run_dir] = Session(run_dir=run_dir)
            return session

    def live_sessions(self) -> int:
        with self._lock:
            return sum(1 for s in self.sessions.values() if not s.finalized)

    def handle_hook(self, payload: dict[str, Any], run_root: Path | None = None, agent_pid: int = 0) -> Any:
        from cordon.wrapper.hook import build_marker, event_kind, session_id_of
        from cordon.wrapper.sampler import agent_pid_path, identity_matches, read_pid_file, write_pid_file
        from cordon.wrapper.schema import MARKERS_FILENAME, append_jsonl

        kind = event_kind(payload)
        if kind is None:
            return None
        now = time.time()
        run_dir = Path(run_root or self.default_run_root) / session_id_of(payload)
        run_dir.mkdir(parents=True, exist_ok=True)
        session = self._session(run_dir)
        session.last_seen = now

        with session.lock:
            if agent_pid and agent_pid != session.agent_pid:
                session.agent_pid = agent_pid
                try:
                    write_pid_file(agent_pid_path(run_dir), agent_pid)
                except OSError:
                    log_failure(self.log, "could not cache agent pid", run_dir=str(run_dir))
            if not session.agent_pid:
                # A daemon restarted mid-session still knows the agent from the run's agent.pid.
                cached = read_pid_file(agent_pid_path(run_dir))
                if cached is not None and identity_matches(*cached):
                    session.agent_pid = cached[0]

            # No hook process exists to time, so the window closes when the event arrives.
            marker = build_marker(payload, now, session.agent_pid, spawn_ts=now if kind == "post" else 0.0)
            append_jsonl(run_dir / MARKERS_FILENAME, marker)

            if kind in ("start", "pre"):
                self._ensure_sampler(session)
            elif kind == "end":
                threading.Thread(target=self._finish, args=(session,), name=f"finalize-{run_dir.name}", daemon=True).start()
        return marker

    def _ensure_sampler(self, session: Session) -> None:
        if not session.agent_pid or (session.sampler is not None and session.sampler.is_alive()):
            return
        session.stop.clear()
        session.sampler = threading.Thread(
            target=self._sample, args=(session,), name=f"sampler-{session.run_dir.name}", daemon=True
        )
        session.sampler.start()
        self.log.info("sampling | run_dir=%s agent_pid=%s", session.run_dir, session.agent_pid)

    def _sample(self, session: Session) -> None:
        from cordon.wrapper.sampler import run_sampler, stop_file

        stop_file(session.run_dir).unlink(missing_ok=True)
        try:
            run_sampler(session.run_dir, session.agent_pid, self.interval, stop_event=session.stop)
        except Exception:
            log_failure(self.log, "sampler thread failed", run_dir=str(session.run_dir))

    def _finish(self, session: Session) -> None:
        from cordon.wrapper.finalize import write_run_report

        session.stop.set()
        if session.sampler is not None:
            session.sampler.join(FINALIZE_JOIN_S)
        with session.lock:
            if session.finalized:
                return
            session.finalized = True
        try:
            out = write_run_report(session.run_dir)
            self.log.info("report written | path=%s", out)
        except Exception:
            log_failure(self.log, "finalize failed", run_dir=str(session.run_dir))

    # -- housekeeping --------------------------------------------------------------------------

    def reap(self, now: float | None = None) -> bool:
        """Finalize sessions whose sampler ended on its own (agent gone, idle). False = time to exit."""
        now = time.time() if now is None else now
        with self._lock:
            sessions = list(self.sessions.values())
        live = False
        for session in sessions:
            if session.finalized:
                continue
            sampling = session.sampler is not None and session.sampler.is_alive()
            if session.sampler is not None and not sampling:
                self._finish(session)  # the agent exited or went idle without a SessionEnd
                continue
            live = live or sampling or now - session.last_seen < self.idle_exit_s
        if live:
            self._last_live = now
        return now - self._last_live < self.idle_exit_s

    def shutdown(self) -> None:
        with self._lock:
            sessions = [s for s in self.sessions.values() if not s.finalized]
        for session in sessions:
            self._finish(session)


def _handler(daemon: Daemon) -> type[BaseHTTPRequestHandler]:
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = f"cordon/{__version__}"

        def log_message(self, format: str, *args: Any) -> None:  # silence the stderr access log
            daemon.log.debug("http | " + format, *args)

        def _reply(self, code: int, body: dict[str, Any] | None = None) -> None:
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _from_browser(self) -> bool:
            # Only agents talk to Cordon. A web page can reach loopback, but always sends Origin.
            return self.headers.get("Origin") is not None

        def do_GET(self) -> None:  # noqa: N802
            if self._from_browser() or urlsplit(self.path).path != HEALTH_PATH:
                self._reply(404)
                return
            body = {"service": "cordon", "version": __version__, "pid": os.getpid(), "sessions": daemon.live_sessions()}
            self._reply(200, body)

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            if self._from_browser() or url.path != HOOK_PATH:
                self._reply(404)
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._reply(415)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reply(413 if length > MAX_BODY else 400)
                return
            query = parse_qs(url.query)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8-sig"))
                run_root = query.get("run_root", [None])[0]
                agent_pid = int(query.get("agent_pid", ["0"])[0] or 0)
                daemon.handle_hook(payload, Path(run_root) if run_root else None, agent_pid)
            except Exception:
                # Never fail the agent's tool call over a recording problem.
                log_failure(daemon.log, "hook event failed, agent unaffected", path=self.path)
            self._reply(200)

    return Handler


def state_path() -> Path:
    return host.user_data_dir() / STATE_FILENAME


def make_server(daemon: Daemon, port_: int) -> ThreadingHTTPServer:
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", port_), _handler(daemon))
    server.daemon_threads = True
    return server


def serve(port_: int | None = None, run_root: Path | None = None, idle_exit_s: float = DEFAULT_IDLE_EXIT_S) -> int:
    from cordon.wrapper.cli import user_run_root

    port_ = port() if port_ is None else port_
    data_dir = host.user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    configure(log_path=data_dir / LOG_FILENAME, to_stderr=False)
    daemon = Daemon(run_root or user_run_root(), idle_exit_s=idle_exit_s)
    try:
        server = make_server(daemon, port_)
    except OSError:
        # Another daemon won the race for the port; that one serves everyone.
        daemon.log.info("port %s busy, assuming a daemon is already running", port_)
        return 0
    state_path().write_text(json.dumps({"pid": os.getpid(), "port": port_, "version": __version__}), encoding="utf-8")
    daemon.log.info("daemon listening | port=%s pid=%s version=%s", port_, os.getpid(), __version__)

    def reaper() -> None:
        while daemon.reap():
            time.sleep(REAP_EVERY_S)
        daemon.log.info("no live agents for %.0fs, exiting", daemon.idle_exit_s)
        server.shutdown()

    threading.Thread(target=reaper, name="reaper", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        daemon.shutdown()
        try:
            state_path().unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def start_detached(port_: int | None = None, wait_s: float = 5.0) -> bool:
    """Start `cordon serve` with no window and wait until it answers /health."""
    command = [sys.executable, "-m", "cordon.wrapper.cli", "serve"]
    if port_ is not None:
        command += ["--port", str(port_)]
    data_dir = host.user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "daemon.stderr").open("ab") as err:
        host.popen_detached(command, str(data_dir), err)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if health(port_, timeout=0.3) is not None:
            return True
        time.sleep(0.1)
    return False


def ensure(run_root: Path | str | None = None, port_: int | None = None, stdin: Any = None) -> int:
    """SessionStart command hook: make sure the daemon runs, then hand it this session and its agent pid."""
    from cordon.wrapper.sampler import resolve_agent_root

    if os.environ.get("CORDON_DISABLE"):  # hook.ENV_DISABLE, without importing the hook module
        return 0
    # SessionStart stdout is added to the agent's context: log to a file, print nothing.
    data_dir = host.user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    configure(log_path=data_dir / LOG_FILENAME, to_stderr=False)
    log = get_logger("daemon")
    stdin = sys.stdin if stdin is None else stdin
    try:
        raw = stdin.read().lstrip("﻿")
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        log_failure(log, "could not read SessionStart payload")
        return 0
    agent_pid = resolve_agent_root()
    if (health(port_) is not None or start_detached(port_)) and post_hook(payload, port_, run_root, agent_pid):
        return 0
    # No daemon (port blocked, loopback firewalled): record the old way, one process per session.
    log.warning("daemon unreachable, falling back to the per-session sampler")
    from cordon.wrapper import hook as hook_module

    hook_module.handle(payload, run_root=Path(run_root) if run_root else None)
    return 0
