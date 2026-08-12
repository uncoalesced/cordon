# Cordon

Tool-call-granularity resource characterization and control for AI coding agents.

Existing resource controllers see "a subprocess." Cordon sees "a `pytest` run that needs
500MB" versus "a `git status` that needs 13MB" — and eventually acts on the difference.
Grounded in AgentCgroup (arXiv 2602.09345) and AgentSight (arXiv 2508.02736); see
[CLAUDE.md](CLAUDE.md) for the full spec.

## Status

**Stage 1 — characterization.** Measurement only: no cgroups, no eBPF, no root, cross-platform.
Stages 2a (`sched_ext`, CPU) and 2b (`memcg_bpf_ops`, memory) require Linux 6.12+ and are not started.

## Install

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Use

Install the hooks into the repo where the agent will actually do its work:

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo
```

Run the agent. Cordon writes a per-session sample stream and marker log under `runs/`.
Then reduce those into one JSON line per tool call:

```
.venv\Scripts\cordon.exe reduce --run-dir runs\<session-id>
```

## Layout

```
features/wrapper/   sampler, hook entrypoint, reducer, JSON-lines schema
features/analysis/  characterization passes over reduced tool-call records
docs/               design notes and findings
tests/              pytest suite
```

## Docs

- [docs/stage1-design.md](docs/stage1-design.md) — how interception and sampling actually work, and why
- `docs/stage1-findings.md` — comparison against the paper's numbers (written once data exists)
