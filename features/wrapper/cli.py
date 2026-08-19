# Engineered by uncoalesced

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from features.analysis.dataset import load_dataset
from features.analysis.metrics import BURST_THRESHOLD_MB, analyze_dataset
from features.analysis.report import render_report
from features.control import contention, probe as probe_module
from features.control.guard import run_guarded
from features.control.intent import ENV_HINT as INTENT_ENV
from features.wrapper import agents
from features.wrapper.logging_setup import configure, get_logger, log_failure
from features.wrapper.reduce import reduce_run
from features.wrapper.schema import DEFAULT_INTERVAL_S, RUN_LOG_FILENAME

# features.wrapper.sampler, .hook, and .wrap import psutil at module scope, and psutil does not
# build on every host Cordon has to report on. They are imported inside the commands that need
# them so that `cordon control probe` — the command whose job is to run first on an unknown box
# — does not require psutil to answer. See docs/stage2-host-audit.md.

HOOK_EVENTS = agents.NESTED_EVENTS[agents.CLAUDE_CODE]


def _hook_command() -> str:
    script = Path(sys.executable).with_name("cordon.exe" if os.name == "nt" else "cordon")
    if script.exists():
        return f'"{script}" hook'
    return f'"{sys.executable}" -m features.wrapper.cli hook'


def hook_settings() -> dict[str, Any]:
    return agents.nested_settings(agents.CLAUDE_CODE, _hook_command())


def _merge_hooks(existing: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    return agents.merge_nested(existing, additions)


def _read_json(path: Path, log: Any) -> dict[str, Any] | None:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log_failure(log, "existing settings unreadable, refusing to overwrite", path=str(path))
        return None


def cmd_sample(args: argparse.Namespace) -> int:
    from features.wrapper.sampler import resolve_agent_root, run_sampler

    run_dir = Path(args.run_dir)
    configure(log_path=run_dir / RUN_LOG_FILENAME, level=logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("cli")

    interval = DEFAULT_INTERVAL_S if args.interval is None else args.interval
    pid = args.pid if args.pid else resolve_agent_root()
    log.info("sampling | run_dir=%s pid=%s interval=%s", run_dir, pid, interval)

    try:
        written = run_sampler(run_dir, root_pid=pid, interval=interval, max_duration_s=args.max_duration)
    except Exception:
        log_failure(log, "sampler aborted", run_dir=str(run_dir), pid=pid, interval=interval)
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

    agent = args.agent
    command = _hook_command()
    outputs: list[tuple[Path, str]] = []  # (path, rendered text) to preview or write, in order

    if agent in agents.NESTED_EVENTS:
        settings_path = agents.settings_path(agent, Path(args.target))
        existing = _read_json(settings_path, log)
        if existing is None:
            return 1
        merged = agents.merge_nested(existing, agents.nested_settings(agent, command))
        outputs.append((settings_path, json.dumps(merged, indent=2) + "\n"))
        if agent == agents.CODEX:
            config_path = agents.codex_config_path(Path(args.target))
            existing_toml = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
            outputs.append((config_path, agents.ensure_codex_feature_flag(existing_toml)))
    elif agent == agents.CURSOR:
        settings_path = agents.settings_path(agent, Path(args.target))
        existing = _read_json(settings_path, log)
        if existing is None:
            return 1
        merged = agents.merge_cursor(existing, agents.cursor_settings(command))
        outputs.append((settings_path, json.dumps(merged, indent=2) + "\n"))
    elif agent == agents.HERMES:
        import yaml

        settings_path = agents.settings_path(agent, Path(args.target))
        existing_hermes: dict[str, Any] = {}
        if settings_path.exists():
            try:
                existing_hermes = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                log_failure(log, "existing hermes config unreadable, refusing to overwrite", path=str(settings_path))
                return 1
        merged = agents.merge_hermes(existing_hermes, agents.hermes_hooks_block(command))
        outputs.append((settings_path, yaml.safe_dump(merged, sort_keys=False)))
    else:
        log.error("unknown agent %r", agent)
        return 1

    if not args.write:
        preview = "\n".join(f"# dry run: would write {path}\n{text}" for path, text in outputs)
        print(preview + "# re-run with --write to apply")
        return 0

    for path, text in outputs:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            log_failure(log, "could not write settings", path=str(path))
            return 1
        log.info("hooks installed | path=%s", path)

    if agent == agents.HERMES:
        log.info("hermes hooks require one-time consent: run `hermes hooks` to review and trust them")
    if agent == agents.CODEX:
        log.info("codex hooks require one-time trust: run `/hooks` inside codex to review and trust them")

    return 0


def cmd_hook(_args: argparse.Namespace) -> int:
    from features.wrapper import hook as hook_module

    return hook_module.main()


def cmd_wrap(args: argparse.Namespace) -> int:
    from features.wrapper.wrap import run_wrap

    configure(level=logging.DEBUG if args.verbose else logging.INFO, to_stderr=False)
    log = get_logger("cli")

    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else list(args.argv)
    if not argv:
        log.error("wrap needs a command after --")
        return 2

    try:
        result = run_wrap(argv, interval=args.interval)
    except Exception:
        log_failure(log, "wrap aborted", argv=argv)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return result.returncode


def cmd_control_probe(args: argparse.Namespace) -> int:
    configure(level=logging.DEBUG if args.verbose else logging.INFO, to_stderr=False)
    capabilities = probe_module.probe()
    if args.json:
        print(json.dumps([c.to_dict() for c in capabilities], indent=2, sort_keys=True))
    else:
        print(probe_module.render(capabilities))
    return 0 if probe_module.enforcement_tier(capabilities) != "none" else 1


def cmd_control_run(args: argparse.Namespace) -> int:
    configure(level=logging.DEBUG if args.verbose else logging.INFO, to_stderr=False)
    log = get_logger("cli")

    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else list(args.argv)
    if not argv:
        log.error("control run needs a command after --")
        return 2

    try:
        result = run_guarded(
            argv,
            hint=args.hint,
            timeout=args.timeout,
            record_path=Path(args.record) if args.record else None,
        )
    except Exception:
        log_failure(log, "guarded run aborted", argv=argv, hint=args.hint)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=repr))
    return result.returncode


def cmd_control_contend(args: argparse.Namespace) -> int:
    configure(level=logging.DEBUG if args.verbose else logging.INFO, to_stderr=False)
    log = get_logger("cli")

    try:
        result = contention.run_contention(high=args.high, low=args.low, work=args.work)
    except Exception:
        log_failure(log, "contention experiment aborted", high=args.high, low=args.low, work=args.work)
        return 1

    report = json.dumps(result.to_dict(), indent=2, sort_keys=True) if args.json else contention.render(result)

    if args.out:
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report + "\n", encoding="utf-8")
        except OSError:
            log_failure(log, "could not write contention report", path=str(out_path))
            return 1
        return 0

    print(report)
    return 0


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

    install = subparsers.add_parser("install-hooks", help="print or write agent hook settings")
    install.add_argument("--target", required=True, help="repo to install into (ignored for --agent hermes, which is user-global)")
    install.add_argument("--agent", choices=agents.AGENT_CHOICES, default=agents.CLAUDE_CODE)
    install.add_argument("--write", action="store_true")
    install.set_defaults(func=cmd_install_hooks)

    hook_parser = subparsers.add_parser("hook", help="hook entrypoint; reads one JSON payload on stdin")
    hook_parser.set_defaults(func=cmd_hook)

    wrap = subparsers.add_parser(
        "wrap", help="run an agent with no hook system (e.g. Aider) as a child and sample it directly"
    )
    wrap.add_argument("--interval", type=float, default=None)
    wrap.add_argument("--json", action="store_true")
    wrap.add_argument("argv", nargs=argparse.REMAINDER)
    wrap.set_defaults(func=cmd_wrap)

    control = subparsers.add_parser("control", help="Stage 2 per-tool-call resource control")
    control_subparsers = control.add_subparsers(dest="control_command", required=True)

    control_probe = control_subparsers.add_parser("probe", help="report kernel enforcement capabilities")
    control_probe.add_argument("--json", action="store_true")
    control_probe.set_defaults(func=cmd_control_probe)

    control_run = control_subparsers.add_parser("run", help="run one command in its own ephemeral cgroup")
    control_run.add_argument("--hint", default=None, help=f"e.g. memory:high; overrides ${INTENT_ENV}")
    control_run.add_argument("--timeout", type=float, default=None)
    control_run.add_argument("--record", default=None, help="append the result to this jsonl file")
    control_run.add_argument("--json", action="store_true")
    control_run.add_argument("argv", nargs=argparse.REMAINDER)
    control_run.set_defaults(func=cmd_control_run)

    control_contend = control_subparsers.add_parser("contend", help="synthetic CPU contention, unguarded vs guarded")
    control_contend.add_argument("--high", type=int, default=1)
    control_contend.add_argument("--low", type=int, default=None)
    control_contend.add_argument("--work", type=int, default=contention.DEFAULT_WORK)
    control_contend.add_argument("--out", default=None)
    control_contend.add_argument("--json", action="store_true")
    control_contend.set_defaults(func=cmd_control_contend)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
