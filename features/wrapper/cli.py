# Engineered by uncoalesced

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from features.adapters import ADAPTERS, DEFAULT_ADAPTER, get_adapter
from features.adapters.claude_code import CONFIG_EVENTS as HOOK_EVENTS
from features.analysis.dataset import load_dataset
from features.analysis.metrics import BURST_THRESHOLD_MB, analyze_dataset
from features.analysis.report import render_report
from features.wrapper import hook as hook_module
from features.wrapper.logging_setup import configure, get_logger, log_failure
from features.wrapper.reduce import reduce_run
from features.wrapper.sampler import DEFAULT_INTERVAL_S, resolve_agent_root, run_sampler
from features.wrapper.schema import RUN_LOG_FILENAME


def _hook_command(tool: str = DEFAULT_ADAPTER) -> str:
    script = Path(sys.executable).with_name("cordon.exe" if os.name == "nt" else "cordon")
    if script.exists():
        return f'"{script}" hook --tool {tool}'
    return f'"{sys.executable}" -m features.wrapper.cli hook --tool {tool}'


def hook_settings(tool: str = DEFAULT_ADAPTER) -> dict[str, Any]:
    adapter = get_adapter(tool)
    if adapter.build_settings is None:
        return {}
    return adapter.build_settings(_hook_command(adapter.name))


def _merge_hooks(existing: dict[str, Any], additions: dict[str, Any], tool: str = DEFAULT_ADAPTER) -> dict[str, Any]:
    return get_adapter(tool).merge(existing, additions)


def cmd_sample(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    configure(log_path=run_dir / RUN_LOG_FILENAME, level=logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("cli")

    pid = args.pid if args.pid else resolve_agent_root()
    log.info("sampling | run_dir=%s pid=%s interval=%s", run_dir, pid, args.interval)

    try:
        written = run_sampler(run_dir, root_pid=pid, interval=args.interval, max_duration_s=args.max_duration)
    except Exception:
        log_failure(log, "sampler aborted", run_dir=str(run_dir), pid=pid, interval=args.interval)
        return 1

    log.info("sampling finished | samples=%s", written)
    return 0


def cmd_reduce(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    configure(log_path=run_dir / RUN_LOG_FILENAME, level=logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("cli")

    try:
        result = reduce_run(run_dir, task_id=args.task_id)
    except Exception:
        log_failure(log, "reduction aborted", run_dir=str(run_dir), task_id=args.task_id)
        return 1

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    configure(level=logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("cli")

    try:
        runs = load_dataset(Path(args.runs))
        dataset = analyze_dataset(runs, burst_threshold_mb=args.burst_threshold)
    except Exception:
        log_failure(log, "analysis aborted", runs=str(args.runs), burst_threshold=args.burst_threshold)
        return 1

    if args.json:
        print(json.dumps(dataset.to_dict(), indent=2, sort_keys=True, default=repr))
        return 0

    report = render_report(dataset, title=args.title)

    if args.out:
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report, encoding="utf-8")
        except OSError:
            log_failure(log, "could not write report", path=str(out_path))
            return 1
        log.info("report written | path=%s runs=%s calls=%s", out_path, dataset.n_runs, dataset.n_toolcalls)
        return 0

    print(report)
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    configure(level=logging.INFO)
    log = get_logger("cli")

    try:
        adapter = get_adapter(args.tool)
    except KeyError as exc:
        log.error("%s", exc)
        return 2

    if not adapter.writes_config:
        print(_snippet_for(adapter))
        return 0

    settings_path = adapter.settings_path_for(args.target)
    additions = adapter.build_settings(_hook_command(adapter.name))

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log_failure(log, "existing settings unreadable, refusing to overwrite", path=str(settings_path))
            return 1

    merged = adapter.merge(existing, additions)
    rendered = json.dumps(merged, indent=2)

    banner = _caveat_banner(adapter)
    if not args.write:
        print(f"{banner}# dry run: would write {settings_path}\n# re-run with --write to apply\n{rendered}")
        return 0

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(rendered + "\n", encoding="utf-8")
    except OSError:
        log_failure(log, "could not write settings", path=str(settings_path))
        return 1

    if banner:
        print(banner, end="")
    log.info("hooks installed | tool=%s path=%s", adapter.name, settings_path)
    return 0


def _caveat_banner(adapter: Any) -> str:
    if adapter.verification == "live" and not adapter.caveat:
        return ""
    lines = []
    if adapter.verification != "live":
        lines.append(f"# UNVERIFIED ADAPTER ({adapter.name}): built from vendor docs, never run against a real event.")
    for line in _wrap(adapter.caveat):
        lines.append(f"# {line}")
    return "\n".join(lines) + "\n" if lines else ""


def _wrap(text: str, width: int = 96) -> list[str]:
    if not text:
        return []
    import textwrap

    return textwrap.wrap(text, width=width)


def _snippet_for(adapter: Any) -> str:
    banner = _caveat_banner(adapter)
    return (
        f"{banner}# {adapter.name} registers hooks in code, not in a config file.\n"
        "# Add this to the script that builds your agent:\n"
        "#\n"
        "#     from features.adapters.claude_agent_sdk import hook_matchers\n"
        "#     options = ClaudeAgentOptions(hooks=hook_matchers())\n"
    )


def cmd_adapters(args: argparse.Namespace) -> int:
    configure(level=logging.INFO, to_stderr=False)
    rows = [
        {
            "tool": adapter.name,
            "verification": adapter.verification,
            "config": "/".join(adapter.settings_relpath) if adapter.writes_config else "(in code)",
            "description": adapter.description,
            "caveat": adapter.caveat,
        }
        for adapter in sorted(ADAPTERS.values(), key=lambda a: a.name)
    ]

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    width = max(len(row["tool"]) for row in rows)
    print("Cordon interception adapters. 'live' saw a real payload; 'docs' never has.\n")
    for row in rows:
        print(f"  {row['tool'].ljust(width)}  {row['verification'].ljust(5)}  {row['config']}")
        print(f"  {' '.ljust(width)}  {row['description']}")
        for line in _wrap(row["caveat"], width=88):
            print(f"  {' '.ljust(width)}  ! {line}")
        print()
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return hook_module.main(tool=getattr(args, "tool", None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cordon", description="Agent tool-call resource characterization")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("sample", help="sample an agent process tree until it exits")
    sample.add_argument("--run-dir", required=True)
    sample.add_argument("--pid", type=int, default=0)
    sample.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    sample.add_argument("--max-duration", type=float, default=None)
    sample.set_defaults(func=cmd_sample)

    reduce_parser = subparsers.add_parser("reduce", help="join markers and samples into per-tool-call records")
    reduce_parser.add_argument("--run-dir", required=True)
    reduce_parser.add_argument("--task-id", default=None)
    reduce_parser.set_defaults(func=cmd_reduce)

    analyze = subparsers.add_parser("analyze", help="characterize reduced runs against the paper's findings")
    analyze.add_argument("--runs", default="runs")
    analyze.add_argument("--out", default=None)
    analyze.add_argument("--burst-threshold", type=float, default=BURST_THRESHOLD_MB)
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--title", default="Stage 1 — Characterization Findings")
    analyze.set_defaults(func=cmd_analyze)

    install = subparsers.add_parser("install-hooks", help="print or write hook settings for one agent tool")
    install.add_argument("--target", required=True)
    install.add_argument("--tool", default=DEFAULT_ADAPTER, choices=sorted(ADAPTERS))
    install.add_argument("--write", action="store_true")
    install.set_defaults(func=cmd_install_hooks)

    adapters_parser = subparsers.add_parser("adapters", help="list interception adapters and their verification status")
    adapters_parser.add_argument("--json", action="store_true")
    adapters_parser.set_defaults(func=cmd_adapters)

    hook_parser = subparsers.add_parser("hook", help="hook entrypoint; reads one JSON payload on stdin")
    hook_parser.add_argument("--tool", default=DEFAULT_ADAPTER)
    hook_parser.set_defaults(func=cmd_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
