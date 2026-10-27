# Engineered by uncoalesced

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cordon.wrapper import daemon as daemon_module
from cordon.wrapper.finalize import report_path
from cordon.wrapper.schema import MARKERS_FILENAME, SAMPLES_FILENAME, read_jsonl


@pytest.fixture
def served(tmp_path: Path):
    instance = daemon_module.Daemon(tmp_path / "runs", interval=0.05)
    server = daemon_module.make_server(instance, 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield instance, server.server_address[1]
    server.shutdown()
    server.server_close()
    instance.shutdown()


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_health_identifies_cordon(served):
    _, port = served
    body = daemon_module.health(port)
    assert body is not None and body["service"] == "cordon" and body["sessions"] == 0


def test_full_session_over_loopback_writes_markers_samples_and_report(served, tmp_path: Path):
    instance, port = served
    agent = _child()
    try:
        session = "s1"
        start = {"hook_event_name": "SessionStart", "session_id": session, "source": "startup"}
        assert daemon_module.post_hook(start, port, agent_pid=agent.pid)
        pre = {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_use_id": "t1",
        }
        assert daemon_module.post_hook(pre, port)
        run_dir = tmp_path / "runs" / session
        samples = run_dir / SAMPLES_FILENAME
        assert _wait(lambda: samples.exists() and samples.stat().st_size > 0)
        assert daemon_module.post_hook({**pre, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}}, port)
        assert daemon_module.post_hook({"hook_event_name": "SessionEnd", "session_id": session}, port)
        assert _wait(lambda: report_path(run_dir).exists())
    finally:
        agent.kill()
    markers = list(read_jsonl(run_dir / MARKERS_FILENAME))
    assert [m["event"] for m in markers] == ["session_start", "tool_start", "tool_end", "session_end"]
    assert markers[0]["command"] == "startup"  # how the session started is recorded
    assert all(m["agent_pid"] == agent.pid for m in markers)
    assert markers[2]["spawn_ts"] == markers[2]["ts"]  # window closes when the event arrives
    assert _wait(lambda: instance.live_sessions() == 0)


def test_run_root_in_the_query_beats_the_default(served, tmp_path: Path):
    _, port = served
    other = tmp_path / "elsewhere"
    assert daemon_module.post_hook({"hook_event_name": "PreToolUse", "session_id": "q"}, port, run_root=other)
    assert (other / "q" / MARKERS_FILENAME).exists()


def _raw_post(port: int, headers: dict[str, str], body: bytes = b"{}") -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/hook", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_browser_requests_and_non_json_are_refused(served, tmp_path: Path):
    _, port = served
    assert _raw_post(port, {"Content-Type": "application/json", "Origin": "https://evil.example"}) == 404
    assert _raw_post(port, {"Content-Type": "text/plain"}) == 415
    runs = tmp_path / "runs"
    assert not runs.exists() or not any(runs.iterdir())


def test_bad_payload_never_fails_the_agent(served):
    _, port = served
    assert _raw_post(port, {"Content-Type": "application/json"}, b"not json") == 200


def test_unknown_events_are_ignored(served, tmp_path: Path):
    instance, _ = served
    assert instance.handle_hook({"hook_event_name": "Notification", "session_id": "n"}) is None
    assert not (tmp_path / "runs" / "n").exists()


def test_reap_finalizes_a_session_whose_agent_died(served, tmp_path: Path):
    instance, port = served
    agent = _child()
    assert daemon_module.post_hook({"hook_event_name": "SessionStart", "session_id": "gone"}, port, agent_pid=agent.pid)
    run_dir = tmp_path / "runs" / "gone"
    assert _wait(lambda: (run_dir / SAMPLES_FILENAME).exists())
    agent.kill()
    agent.wait()
    session = instance.sessions[run_dir]
    assert _wait(lambda: not session.sampler.is_alive())
    assert instance.reap() is True
    assert report_path(run_dir).exists() and session.finalized


def test_reap_says_exit_after_idle_with_no_live_agents(tmp_path: Path):
    instance = daemon_module.Daemon(tmp_path, idle_exit_s=10)
    assert instance.reap(now=time.time()) is True
    assert instance.reap(now=time.time() + 11) is False


def test_restarted_daemon_recovers_the_agent_from_agent_pid(served, tmp_path: Path):
    from cordon.wrapper.sampler import agent_pid_path, write_pid_file

    instance, port = served
    agent = _child()
    try:
        run_dir = tmp_path / "runs" / "resumed"
        run_dir.mkdir(parents=True)
        write_pid_file(agent_pid_path(run_dir), agent.pid)
        assert daemon_module.post_hook({"hook_event_name": "PreToolUse", "session_id": "resumed"}, port)
        assert _wait(lambda: instance.sessions[run_dir].agent_pid == agent.pid)
    finally:
        agent.kill()


def _no_daemon(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(daemon_module, "health", lambda *a, **k: None)
    monkeypatch.setattr(daemon_module, "start_detached", lambda *a, **k: False)
    monkeypatch.setattr("cordon.wrapper.sampler.resolve_agent_root", lambda *a, **k: 1)
    monkeypatch.setattr(daemon_module.host, "user_data_dir", lambda *a, **k: tmp_path / "data")


def test_ensure_registers_with_a_running_daemon(served, tmp_path: Path, monkeypatch):
    _, port = served
    monkeypatch.setattr("cordon.wrapper.sampler.resolve_agent_root", lambda *a, **k: 4242)
    monkeypatch.setattr(daemon_module.host, "user_data_dir", lambda *a, **k: tmp_path / "data")
    stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "e1"}))
    assert daemon_module.ensure(run_root=tmp_path / "r", port_=port, stdin=stdin) == 0
    markers = list(read_jsonl(tmp_path / "r" / "e1" / MARKERS_FILENAME))
    assert markers[0]["agent_pid"] == 4242


def test_ensure_falls_back_to_the_command_path_without_a_daemon(tmp_path: Path, monkeypatch):
    seen = {}
    _no_daemon(monkeypatch, tmp_path)
    monkeypatch.setattr("cordon.wrapper.hook.handle", lambda payload, run_root=None: seen.update(payload=payload, root=run_root))
    stdin = io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "f"}))
    assert daemon_module.ensure(run_root=tmp_path, port_=1, stdin=stdin) == 0
    assert seen["payload"]["session_id"] == "f" and seen["root"] == tmp_path


def test_ensure_prints_nothing_because_sessionstart_stdout_reaches_the_model(tmp_path: Path, monkeypatch, capsys):
    _no_daemon(monkeypatch, tmp_path)
    monkeypatch.setattr("cordon.wrapper.hook.handle", lambda *a, **k: None)
    daemon_module.ensure(run_root=tmp_path, port_=1, stdin=io.StringIO("{}"))
    assert capsys.readouterr().out == ""


def test_hook_url_carries_the_run_root():
    url = daemon_module.hook_url(5, Path("C:/a b/runs"))
    assert url.startswith("http://127.0.0.1:5/hook?run_root=")
    assert "a+b" in url or "a%20b" in url
