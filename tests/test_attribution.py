# Engineered by uncoalesced

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from cordon.analysis.dataset import load_dataset
from cordon.analysis.metrics import analyze_dataset, classify_bash
from cordon.analysis.report import render_report
from cordon.lockfile import append_line
from cordon.wrapper.hookconfig import HookMatcher, collect, strip_shell, write_snapshot
from cordon.wrapper.reduce import active_seconds, exe_label, reduce_run
from cordon.wrapper.schema import (
    EVENT_SESSION_START,
    EVENT_TOOL_END,
    EVENT_TOOL_START,
    MARKERS_FILENAME,
    PROCS_FILENAME,
    SAMPLES_FILENAME,
    JsonlWriter,
    Marker,
    Sample,
    read_jsonl,
)

AGENT = 100
BASH = r"C:\Program Files\Git\bin\bash.exe"
INLINE_HOOK = (
    "node -e \"const p=require('path');const r=process.env.CLAUDE_PLUGIN_ROOT;require(p.join(r,'scripts/hooks/run.js'))\""
)
SNAPSHOT_SHELL = f"{BASH} -c source '/c/u/.claude/shell-snapshots/snapshot-bash-1.sh' && python train.py"


def _write(path: Path, rows: list) -> None:
    with JsonlWriter(path) as writer:
        for row in rows:
            writer.write(row)


def _marker(event: str, ts: float, key: str = "", tool: str = "", command: str = "") -> Marker:
    return Marker(event=event, ts=ts, session_id="s", call_key=key, tool_type=tool, command=command, cwd="", agent_pid=AGENT)


def _proc(pid: int, ppid: int, created: float, cmd: str) -> dict:
    return {"pid": pid, "ppid": ppid, "created": created, "cmd": cmd}


def _claude_run(run_dir: Path, hooks: list[dict] | None = None) -> None:
    """A Claude Code session: a Read, a Bash call running python, a plan approval wait.

    Peer hooks fire at every tool event, exactly when the call opens, and one of them forks a
    heavy background observer. Only the snapshot shell and its python belong to the Bash call.
    """
    run_dir.mkdir(parents=True)
    _write(
        run_dir / MARKERS_FILENAME,
        [
            _marker(EVENT_SESSION_START, 0.0, command="startup"),
            _marker(EVENT_TOOL_START, 10.0, "read", "Read", "/repo/a.py"),
            _marker(EVENT_TOOL_END, 10.5, "read", "Read", "/repo/a.py"),
            _marker(EVENT_TOOL_START, 20.0, "bash", "Bash", "cd /repo && PYTHONIOENCODING=utf-8 python train.py"),
            _marker(EVENT_TOOL_END, 30.0, "bash", "Bash", "cd /repo && python train.py"),
            _marker(EVENT_TOOL_START, 40.0, "plan", "ExitPlanMode", "{}"),
            _marker(EVENT_TOOL_END, 100.0, "plan", "ExitPlanMode", "{}"),
            _marker(EVENT_TOOL_START, 101.0, "blocked", "Write", "/repo/b.py"),
        ],
    )
    _write(
        run_dir / PROCS_FILENAME,
        [
            _proc(201, AGENT, 10.01, f"{BASH} -c {INLINE_HOOK}"),  # ecc-style inline hook on the Read
            _proc(202, 201, 10.1, r"C:\Program Files\nodejs\node.exe -e const p=require('path')"),
            _proc(203, 202, 10.2, "bash.exe /c/u/.claude/plugins/cache/ecc/ecc/1/hooks/observe.sh"),
            _proc(210, AGENT, 20.02, f"{BASH} -c rtk hook claude"),
            _proc(220, AGENT, 22.0, SNAPSHOT_SHELL),
            _proc(221, 220, 22.5, r"C:\repo\.venv\Scripts\python.exe train.py"),
            _proc(230, AGENT, 25.0, r"C:\Windows\system32\cmd.exe /d /s /c npx -y server-pdf --stdio"),
        ],
    )
    samples = []
    for i in range(0, 1100):
        t = round(i * 0.1, 3)
        kids: dict[str, list[float]] = {}
        if 10.1 <= t <= 60.0:
            kids["203"] = [400.0, 50.0]  # observer: busy, outlives the Read by far
        if 20.02 <= t <= 20.3:
            kids["210"] = [10.0, 5.0]
        if 22.0 <= t <= 29.9:
            kids["220"] = [5.0, 1.0]
        if 22.5 <= t <= 29.9:
            kids["221"] = [1000.0, 100.0]
        if t >= 25.0:
            kids["230"] = [80.0, 1.0]
        samples.append(Sample(t=t, mem_mb=1500.0, cpu_pct=20.0, n_procs=10, kids=kids or None))
    _write(run_dir / SAMPLES_FILENAME, samples)
    if hooks is not None:
        (run_dir / "hooks.json").write_text(json.dumps({"cwd": "", "hooks": hooks}), encoding="utf-8")


@pytest.fixture
def claude_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "s"
    _claude_run(run_dir, hooks=[{"source": "plugin ecc", "event": "PreToolUse", "command": INLINE_HOOK}])
    return run_dir


def _by_tool(result) -> dict:
    return {r.tool_type: r for r in result.records}


def test_peer_hooks_are_not_charged_to_the_call_they_start_with(claude_run: Path):
    result = reduce_run(claude_run, write=False)
    calls = _by_tool(result)
    assert result.attribution_mode == "positive"
    assert calls["Read"].own_peak_mb == 0.0 and calls["Read"].own_procs == 0
    assert calls["ExitPlanMode"].own_peak_mb == 0.0


def test_bash_call_owns_its_snapshot_shell_and_children(claude_run: Path):
    bash = _by_tool(reduce_run(claude_run, write=False))["Bash"]
    assert bash.own_procs == 2
    assert bash.own_peak_mb == pytest.approx(1005.0)


def test_leftover_processes_are_grouped_by_what_started_them(claude_run: Path):
    groups = {(g["kind"], g["label"]): g for g in reduce_run(claude_run, write=False).process_groups}
    ecc = groups[("hook", "plugin ecc: run (PreToolUse)")]
    assert ecc["procs"] == 3 and ecc["peak_mb"] == 400.0 and ecc["cpu_s"] > 20
    assert ("hook", "rtk hook") in groups  # no config entry: caught by the hook-subcommand pattern
    assert ("other", "cmd npx") in groups  # an MCP server the agent started
    assert not any(kind == "background" for kind, _ in groups)


def test_tool_processes_that_outlive_their_call_are_background(tmp_path: Path):
    run_dir = tmp_path / "s"
    _claude_run(run_dir, hooks=[])
    rows = list(read_jsonl(run_dir / SAMPLES_FILENAME))
    for row in rows:  # train.py keeps running after the Bash call returned (run_in_background)
        if 30.0 < row["t"] <= 80.0:
            row.setdefault("kids", {})["221"] = [900.0, 100.0]
    (run_dir / SAMPLES_FILENAME).unlink()
    _write(run_dir / SAMPLES_FILENAME, rows)

    groups = reduce_run(run_dir, write=False).process_groups
    background = [g for g in groups if g["kind"] == "background"]
    assert background and background[0]["label"].startswith("cd /repo && PYTHONIOENCODING")
    assert background[0]["peak_mb"] == 900.0 and background[0]["procs"] == 1


def test_runs_without_tool_fingerprints_keep_window_attribution_minus_hooks(tmp_path: Path):
    run_dir = tmp_path / "s"
    run_dir.mkdir()
    _write(
        run_dir / MARKERS_FILENAME,
        [_marker(EVENT_TOOL_START, 1.0, "k", "run_shell", "python work.py"), _marker(EVENT_TOOL_END, 5.0, "k", "run_shell")],
    )
    _write(
        run_dir / PROCS_FILENAME,
        [_proc(301, AGENT, 1.01, "/bin/sh -c rtk hook codex"), _proc(302, AGENT, 1.5, "python work.py")],
    )
    _write(
        run_dir / SAMPLES_FILENAME,
        [Sample(t=1.0 + i * 0.25, mem_mb=500.0, cpu_pct=1.0, kids={"301": [50.0, 1.0], "302": [200.0, 9.0]}) for i in range(16)],
    )
    (run_dir / "hooks.json").write_text(json.dumps({"hooks": []}), encoding="utf-8")

    result = reduce_run(run_dir, write=False)
    assert result.attribution_mode == "window"
    assert result.records[0].own_procs == 1 and result.records[0].own_peak_mb == 200.0


def test_summary_counts_calls_that_never_finished_and_active_time(claude_run: Path):
    result = reduce_run(claude_run)
    summary = json.loads((claude_run / "summary.json").read_text(encoding="utf-8"))
    assert summary["unpaired_by_tool"] == {"Write": 1}
    assert summary["empty"] is False
    # The plan wait is not tool time.
    assert result.tool_time_s == pytest.approx(10.5)


def test_failed_shell_calls_without_an_end_event_are_closed_when_their_shell_ran(tmp_path: Path):
    run_dir = tmp_path / "s"
    _claude_run(run_dir, hooks=[])
    markers = [m for m in read_jsonl(run_dir / MARKERS_FILENAME) if m.get("call_key") != "bash" or m["event"] == EVENT_TOOL_START]
    markers.append(_marker(EVENT_TOOL_START, 35.0, "next", "Read", "/repo/c.py").to_dict())
    (run_dir / MARKERS_FILENAME).unlink()
    _write(run_dir / MARKERS_FILENAME, markers)

    result = reduce_run(run_dir, write=False)
    bash = _by_tool(result)["Bash"]
    assert bash.exit_status == "no end event"
    assert bash.end_ts == 35.0 and bash.own_peak_mb == pytest.approx(1005.0)
    assert result.unpaired_by_tool == {"Write": 1, "Read": 1}  # the blocked Write never ran


def test_active_seconds_drops_idle_gaps_but_keeps_long_tool_calls():
    assert active_seconds([0.0, 5.0, 2000.0, 2001.0], []) == pytest.approx(6.0)
    assert active_seconds([0.0, 5.0, 2000.0, 2001.0], [(5.0, 2000.0)]) == pytest.approx(2001.0)


def test_report_shows_where_the_rest_went(claude_run: Path):
    reduce_run(claude_run)
    report = render_report(analyze_dataset(load_dataset(claude_run)))
    assert "## Where the rest went" in report
    assert "plugin ecc: run (PreToolUse)" in report
    assert "## Calls that never finished" in report and "Write 1" in report
    assert "waits on you" in report


def test_empty_sessions_are_left_out_of_dataset_figures(claude_run: Path):
    reduce_run(claude_run)
    empty = claude_run.parent / "empty"
    empty.mkdir()
    _write(empty / MARKERS_FILENAME, [_marker(EVENT_SESSION_START, 0.0, command="startup")])
    _write(empty / SAMPLES_FILENAME, [Sample(t=0.0, mem_mb=1.0, cpu_pct=0.0), Sample(t=2.0, mem_mb=1.0, cpu_pct=0.0)])
    assert reduce_run(empty).empty

    dataset = analyze_dataset(load_dataset(claude_run.parent))
    assert dataset.n_runs == 1 and dataset.empty_runs == 1


@pytest.mark.parametrize(
    ("command", "category"),
    [
        ("cd E:/repo && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -q", "test"),
        ("cd /repo && for t in tests/test_slab.py; do python $t; done", "test"),
        ("export CUDA_VISIBLE_DEVICES=-1 OMP_NUM_THREADS=1 && .venv/Scripts/python.exe train.py", "python"),
        ("S='C:/tmp/x'; cat $S/log.txt | tail -5", "filesystem"),
        ("$env:PYTHONIOENCODING='utf-8'; & E:\\repo\\.venv\\Scripts\\python.exe run.py", "python"),
        ("$r='E:\\repo'; Get-ChildItem \"$r\\data\" -Directory | Select-Object Name", "filesystem"),
        ("cd /repo && git status && git diff", "vcs"),
        ("cd /repo", "other"),
        ("timeout 590 bash -c 'while ! grep -q done run.log; do sleep 15; done'; tail -5 run.log", "wait"),
        ("$t=0; while (-not (Select-String -Path x.status -Pattern done -Quiet)) { Start-Sleep 20 }", "wait"),
        ("python train.py; sleep 5", "python"),
        ("ls -la && pytest", "test"),
    ],
)
def test_shell_commands_are_categorised_by_what_they_run(command: str, category: str):
    assert classify_bash(command) == category


def test_strip_shell_handles_paths_with_spaces():
    assert strip_shell(f"{BASH} -c rtk hook claude") == "rtk hook claude"
    assert strip_shell('"C:\\Program Files\\Git\\bin\\bash.exe" -c x y') == "x y"
    assert strip_shell("/bin/sh -c echo hi") == "echo hi"
    assert strip_shell("python train.py") == "python train.py"


def test_exe_label_reads_unquoted_windows_paths():
    assert exe_label(r"C:\Program Files\Git\usr\bin\sleep.exe 0.02") == "sleep"
    assert exe_label(r"C:\Windows\system32\cmd.exe /d /s /c npx ^-y^ ^@x/server-pdf^") == "cmd npx"
    assert exe_label(r"\??\C:\Windows\system32\conhost.exe 0x4") == "conhost"


def test_matcher_labels_hooks_that_share_a_launcher():
    launcher = "node -e \"const p=require('path');run('"
    matcher = HookMatcher(
        [
            {"source": "plugin ecc", "event": "PreToolUse", "command": launcher + "a.js')\""},
            {"source": "plugin ecc", "event": "PostToolUse", "command": launcher + "b.js')\""},
        ]
    )
    assert matcher.label(f"{BASH} -c {launcher}") == "plugin ecc: 2 hooks sharing one launcher"


def test_collect_reads_settings_and_enabled_plugin_hooks(tmp_path: Path):
    home = tmp_path / "home"
    claude = home / ".claude"
    plugin = claude / "plugins" / "cache" / "m" / "ecc" / "1"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "node x.js"}]}]}}),
        encoding="utf-8",
    )
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"ecc@m": [{"installPath": str(plugin)}], "off@m": [{"installPath": str(plugin)}]}}),
        encoding="utf-8",
    )
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"ecc@m": True, "off@m": False},
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "rtk hook claude"}]}],
                    "PreToolUse": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:1/hook"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    found = collect(None, home=home)
    assert {(h["source"], h["command"]) for h in found} == {("user settings", "rtk hook claude"), ("plugin ecc", "node x.js")}

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert write_snapshot(run_dir, None, home=home) is not None
    assert len(json.loads((run_dir / "hooks.json").read_text(encoding="utf-8"))["hooks"]) == 2


def _append_many(path: str, writer: int, count: int) -> None:
    line = json.dumps({"writer": writer, "pad": "x" * 300})
    for _ in range(count):
        append_line(Path(path), line)


def test_concurrent_appends_from_many_processes_never_tear_a_line(tmp_path: Path):
    path = tmp_path / "markers.jsonl"
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_append_many, args=(str(path), w, 500)) for w in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(120)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4000
    assert all(json.loads(line)["pad"] == "x" * 300 for line in lines)
