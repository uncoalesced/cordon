# Engineered by uncoalesced

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from features.wrapper import sampler as sampler_module
from features.wrapper.sampler import AGENT_PROCESS_NAMES, TreeSampler, resolve_agent_root, run_sampler, stop_file
from features.wrapper.schema import SAMPLES_FILENAME, JsonlWriter, read_jsonl


@pytest.mark.parametrize(
    "name",
    ["claude", "hermes", "codex", "cursor-agent", "gemini", "aider"],
)
def test_agent_process_names_cover_every_supported_agent(name):
    assert name in AGENT_PROCESS_NAMES
    assert f"{name}.exe" in AGENT_PROCESS_NAMES


def test_resolve_agent_root_returns_a_live_pid():
    pid = resolve_agent_root()
    assert isinstance(pid, int) and pid > 0


def test_resolve_agent_root_on_dead_pid_falls_back_to_that_pid():
    assert resolve_agent_root(start_pid=2**31 - 1) == 2**31 - 1


def test_sample_once_measures_the_current_process():
    sampler = TreeSampler(root_pid=os.getpid())
    sample = sampler.sample_once()
    assert sample.mem_mb > 0
    assert sample.n_procs >= 1
    assert sample.cpu_pct >= 0


@pytest.mark.integration
def test_sample_once_includes_children(busy_child):
    sampler = TreeSampler(root_pid=os.getpid())
    before = sampler.sample_once()
    busy_child(duration=30.0)

    deadline = time.monotonic() + 20.0
    after = sampler.sample_once()
    while after.mem_mb <= before.mem_mb and time.monotonic() < deadline:
        time.sleep(0.05)
        after = sampler.sample_once()

    assert after.n_procs > before.n_procs
    assert after.mem_mb > before.mem_mb


def test_run_stops_on_max_samples(run_dir: Path):
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        sampler = TreeSampler(root_pid=os.getpid(), interval=0.01, writer=writer)
        written = sampler.run(max_samples=5)
    assert written == 5
    assert len(list(read_jsonl(run_dir / SAMPLES_FILENAME))) == 5


def test_run_stops_on_stop_condition():
    sampler = TreeSampler(root_pid=os.getpid(), interval=0.01)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    assert sampler.run(stop_check=stop) == 3


def test_run_exits_immediately_when_root_is_gone():
    sampler = TreeSampler(root_pid=2**31 - 1, interval=0.01)
    assert sampler.run(max_samples=10) == 0


def test_run_survives_a_failing_tick(monkeypatch):
    sampler = TreeSampler(root_pid=os.getpid(), interval=0.01)
    calls = {"n": 0}
    original = TreeSampler.sample_once

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated psutil explosion")
        return original(self)

    monkeypatch.setattr(TreeSampler, "sample_once", flaky)
    assert sampler.run(max_samples=3) == 3
    assert sampler.failed_samples == 1


def test_run_honours_the_interval(run_dir: Path):
    started = time.monotonic()
    with JsonlWriter(run_dir / SAMPLES_FILENAME) as writer:
        TreeSampler(root_pid=os.getpid(), interval=0.05, writer=writer).run(max_samples=4)
    assert time.monotonic() - started >= 0.10


@pytest.mark.integration
def test_run_sampler_writes_stream_and_respects_stop_file(run_dir: Path):
    stop_file(run_dir).touch()
    assert run_sampler(run_dir, root_pid=os.getpid(), interval=0.01) == 0

    stop_file(run_dir).unlink()
    written = run_sampler(run_dir, root_pid=os.getpid(), interval=0.01, max_duration_s=0.2)
    assert written > 0
    rows = list(read_jsonl(run_dir / SAMPLES_FILENAME))
    assert len(rows) == written
    assert all(row["mem_mb"] > 0 for row in rows)


class FakeProc:
    def __init__(self, pid, name, cmdline, parent=None):
        self.pid, self._name, self._cmdline, self._parent = pid, name, cmdline, parent

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def parent(self):
        return self._parent


def chain(*links):
    """links ordered outermost ancestor first; returns a lookup for resolve_agent_root and the hook pid."""
    procs, parent = {}, None
    for pid, (name, cmdline) in enumerate(links, start=100):
        parent = procs[pid] = FakeProc(pid, name, cmdline, parent)
    return procs.__getitem__, parent.pid


HOOK = ("python3", ["python3", "-m", "features.wrapper.cli", "hook"])
SH = ("sh", ["/bin/sh", "-c", "cordon hook"])


@pytest.mark.parametrize(
    "links,expected",
    [
        # Linux npm install, a node MCP server nested between claude and the hook's shell.
        (
            [("bash", ["bash"]), ("node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]),
             ("node", ["node", "/home/u/mcp/server.js"]), SH, HOOK],
            101,
        ),
        # Native claude binary, process named after its version file behind a `claude` symlink.
        ([("zsh", ["-zsh"]), ("2.0.14", ["claude"]), SH, HOOK], 101),
        # python -m aider.
        ([("bash", ["bash"]), ("python3.12", ["python3.12", "-m", "aider"]), SH, HOOK], 101),
        # macOS claude binary under launchd/Terminal.
        ([("launchd", ["/sbin/launchd"]), ("login", ["login"]), ("claude", ["/opt/homebrew/bin/claude"]), SH, HOOK], 102),
        # codex via node.
        ([("fish", ["fish"]), ("node", ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"]), SH, HOOK], 101),
        # Windows: claude.exe.
        ([("explorer.exe", ["explorer.exe"]), ("claude.exe", ["C:\\Users\\u\\claude.exe"]), ("cmd.exe", ["cmd"]), HOOK], 101),
        # Linux comm truncated to 15 chars ("cursor-agent" fits, but a longer variant would not).
        ([("bash", ["bash"]), ("cursor-agent", ["cursor-agent"]), SH, HOOK], 101),
    ],
)
def test_resolve_agent_root_scores_fake_chains(links, expected):
    lookup, hook_pid = chain(*links)
    assert resolve_agent_root(start_pid=hook_pid, lookup=lookup) == expected


def test_bare_node_wins_only_when_nothing_better_exists():
    lookup, hook_pid = chain(("bash", ["bash"]), ("node", ["node", "/srv/app.js"]), SH, HOOK)
    assert resolve_agent_root(start_pid=hook_pid, lookup=lookup) == 101


def test_no_agent_like_ancestor_falls_back_to_the_furthest():
    lookup, hook_pid = chain(("init", ["init"]), ("bash", ["bash"]), SH, HOOK)
    assert resolve_agent_root(start_pid=hook_pid, lookup=lookup) == 100


def test_agent_pid_env_override_wins(monkeypatch):
    monkeypatch.setenv(sampler_module.ENV_AGENT_PID, "31337")
    assert resolve_agent_root() == 31337
    monkeypatch.setenv(sampler_module.ENV_AGENT_PID, "not-a-pid")
    assert resolve_agent_root() > 0


@pytest.mark.parametrize(
    "name,cmdline,score",
    [
        ("node", ["node", "x/@anthropic-ai/claude-code/cli.js"], sampler_module.SCORE_ARGV),
        ("bun", ["bun", "x/@google/gemini-cli/index.js"], sampler_module.SCORE_ARGV),
        ("deno", ["deno", "run", "codex.ts"], sampler_module.SCORE_ARGV),
        ("claude.exe", [], sampler_module.SCORE_EXECUTABLE),
        ("hermes", ["hermes"], sampler_module.SCORE_EXECUTABLE),
        ("node.exe", ["node.exe", "server.js"], sampler_module.SCORE_INTERPRETER),
        ("bash", ["bash", "-c", "claude-code"], 0),  # a shell mentioning the agent is not the agent
    ],
)
def test_agent_score(name, cmdline, score):
    assert sampler_module.agent_score(name, cmdline) == score


def test_parse_pss_reads_smaps_rollup():
    text = "55d0-7ffd ---p 00000000 00:00 0 [rollup]\nRss:  2048 kB\nPss:  1536 kB\nPss_Anon: 1000 kB\n"
    assert sampler_module.parse_pss_kb(text) == 1536
    assert sampler_module.parse_pss_kb("Rss: 1 kB\n") is None
    assert sampler_module.parse_pss_kb("Pss: lots kB\n") is None


def test_unique_bytes_per_os(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from features import host

    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "smaps_rollup").write_text("Pss: 10 kB\n")

    def never():  # macOS must not use the minutes-long USS walk
        raise AssertionError("memory_full_info called")

    proc = SimpleNamespace(pid=7, memory_info=lambda: SimpleNamespace(rss=999), memory_full_info=never)
    assert sampler_module.unique_bytes(proc, "linux", str(tmp_path)) == 10 * 1024
    proc.pid = 8  # no smaps_rollup: falls back to rss
    assert sampler_module.unique_bytes(proc, "linux", str(tmp_path)) == 999
    monkeypatch.setattr(host, "darwin_footprint", lambda pid: 55)
    assert sampler_module.unique_bytes(proc, "darwin") == 55
    monkeypatch.setattr(host, "darwin_footprint", lambda pid: None)  # denied / unavailable
    assert sampler_module.unique_bytes(proc, "darwin") == 999
    assert sampler_module.unique_bytes(proc, "windows") == 999
    proc.memory_info = lambda: SimpleNamespace(rss=999, private=321)
    assert sampler_module.unique_bytes(proc, "windows") == 321


def _fake_cgroup(tmp_path: Path, procs: str) -> tuple[str, str]:
    proc_root, cg_root = tmp_path / "proc", tmp_path / "cg"
    (proc_root / "10").mkdir(parents=True)
    (proc_root / "10" / "cgroup").write_text("0::/user.slice/agent.scope\n")
    scope = cg_root / "user.slice" / "agent.scope"
    scope.mkdir(parents=True)
    (scope / "cgroup.procs").write_text(procs)
    (scope / "memory.current").write_text(str(512 * 1024 * 1024))
    return str(proc_root), str(cg_root)


def test_exclusive_cgroup_used_only_when_every_member_is_in_the_tree(tmp_path: Path):
    proc_root, cg_root = _fake_cgroup(tmp_path, "10\n11\n")
    found = sampler_module.exclusive_cgroup(10, {10, 11, 12}, proc_root, cg_root)
    assert found is not None and found.name == "memory.current"
    assert sampler_module.exclusive_cgroup(10, {10}, proc_root, cg_root) is None  # 11 is a stranger
    assert sampler_module.exclusive_cgroup(99, {99}, proc_root, cg_root) is None  # no /proc entry


def test_sampler_records_unique_memory_every_nth_tick_and_carries_it():
    sampler = TreeSampler(root_pid=os.getpid(), unique_every=3)
    samples = [sampler.sample_once() for _ in range(4)]
    assert all(s.mem_mb_unique is not None and s.mem_mb_unique > 0 for s in samples)
    assert samples[1].mem_mb_unique == samples[0].mem_mb_unique == samples[2].mem_mb_unique


def test_sampler_counts_errors_by_type():
    sampler = TreeSampler(root_pid=2**31 - 1)
    assert sampler.sample_once().n_procs == 0
    assert sampler.errors["NoSuchProcess"] == 1


def test_cgroup_memory_is_sampled_when_exclusive(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sampler_module.host, "OS", "linux")
    sampler = TreeSampler(root_pid=os.getpid())
    current = tmp_path / "memory.current"
    current.write_text(str(256 * 1024 * 1024))
    monkeypatch.setattr(sampler_module, "exclusive_cgroup", lambda *_a: current)
    monkeypatch.setattr(sampler_module, "unique_bytes", lambda proc: proc.memory_info().rss)
    assert sampler.sample_once().cg_mem_mb == 256.0
    current.unlink()  # cgroup vanished: stop reading it rather than fail every tick
    assert sampler.sample_once().cg_mem_mb is None


def test_idle_for_uses_marker_mtime(run_dir: Path):
    assert sampler_module.idle_for(run_dir, since=100.0, now=150.0) == 50.0
    (run_dir / "markers.jsonl").write_text("{}\n")
    mtime = (run_dir / "markers.jsonl").stat().st_mtime
    assert sampler_module.idle_for(run_dir, since=0.0, now=mtime + 10) == pytest.approx(10.0)


def test_run_sampler_stops_when_markers_go_idle(run_dir: Path, monkeypatch):
    monkeypatch.setenv(sampler_module.ENV_IDLE_STOP, "0")
    (run_dir / "markers.jsonl").write_text("{}\n")
    started = time.monotonic()
    run_sampler(run_dir, root_pid=os.getpid(), interval=0.01, max_duration_s=10)
    assert time.monotonic() - started < 5


def test_pid_file_round_trip(run_dir: Path):
    path = run_dir / "x.pid"
    sampler_module.write_pid_file(path, os.getpid())
    pid, created = sampler_module.read_pid_file(path)
    assert pid == os.getpid() and sampler_module.identity_matches(pid, created)
    assert not sampler_module.identity_matches(pid, created + 100)
    path.write_text("garbage")
    assert sampler_module.read_pid_file(path) is None

def test_walk_stops_below_init_and_the_macos_kernel():
    # macOS: launchd (pid 1) has the kernel (pid 0) as parent; neither may become the agent root.
    kernel = FakeProc(0, "kernel_task", [])
    launchd = FakeProc(1, "launchd", ["/sbin/launchd"], kernel)
    login = FakeProc(50, "login", ["login"], launchd)
    sh = FakeProc(51, "sh", ["/bin/sh"], login)
    hook = FakeProc(52, *HOOK, sh)
    procs = {p.pid: p for p in (kernel, launchd, login, sh, hook)}
    assert resolve_agent_root(start_pid=52, lookup=procs.__getitem__) == 50


def test_sampler_records_young_children_but_not_cordons_own(tmp_path):
    import subprocess
    import sys

    from features.wrapper.schema import JsonlWriter, read_jsonl

    tool = subprocess.Popen([sys.executable, "-c", "import time; b=b'x'*(30<<20); time.sleep(5)"])
    own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", "features.wrapper.cli"])
    try:
        with JsonlWriter(tmp_path / "procs.jsonl") as procs:
            sampler = TreeSampler(root_pid=os.getpid(), interval=0.05, procs_writer=procs)
            sample = None
            for _ in range(40):  # wait until the tool child has allocated
                sample = sampler.sample_once()
                if sum(m for m, _ in (sample.kids or {}).values()) > 25:
                    break
                time.sleep(0.05)
        # A Windows venv python.exe is a launcher whose child is the real interpreter: the tool's
        # memory can sit in a grandchild, which is exactly why the whole young subtree is kept.
        assert str(tool.pid) in sample.kids and str(own.pid) not in sample.kids
        assert sum(m for m, _ in sample.kids.values()) > 25
        recorded = list(read_jsonl(tmp_path / "procs.jsonl"))
        assert tool.pid in {r["pid"] for r in recorded}
        assert not any("features.wrapper.cli" in r["cmd"] for r in recorded)
    finally:
        for proc in (tool, own):
            proc.kill()
            proc.wait(timeout=5)
