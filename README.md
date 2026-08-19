<p align="center">
  <img src="assets/banner.svg" alt="Cordon — tool-call-granularity resource control for AI coding agents" width="720">
</p>

<p align="center">
  <a href="https://github.com/uncoalesced/cordon/actions/workflows/ci.yml"><img src="https://github.com/uncoalesced/cordon/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-f5c400?style=flat-square&labelColor=0d0d10" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-f5c400?style=flat-square&labelColor=0d0d10" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/stage%201-measuring-f5c400?style=flat-square&labelColor=0d0d10" alt="Stage 1: measuring">
  <img src="https://img.shields.io/badge/stage%202-partial-9a5b00?style=flat-square&labelColor=0d0d10" alt="Stage 2: partial">
  <a href="docs/stage1-design.md"><img src="https://img.shields.io/badge/grounded%20in-AgentCgroup%20%2F%20AgentSight-0d0d10?style=flat-square&labelColor=f5c400" alt="Grounded in AgentCgroup / AgentSight"></a>
</p>

Cordon watches what an AI coding agent does at the level of individual tool calls, not the
container as a whole. To most resource controllers, a `pytest` run and a `git status` are both
just "a subprocess." Cordon tells them apart, because one needs 500MB and the other needs 13MB,
and a single container-wide limit can't serve both well.

Stage 1 measures: it hooks into Claude Code, Codex CLI, Hermes Agent, Cursor CLI, or Gemini CLI
(Aider too, at session granularity), tracks memory and CPU per tool call, and turns that into a
report you can compare against published numbers. Stage 2 acts on what Stage 1 finds, and is
partly built — the per-call cgroup control path and the agent-facing hint protocol work on any
Linux with cgroup v2, while the in-kernel policy layer is still blocked on kernel features that
aren't available yet.

Grounded in AgentCgroup (arXiv 2602.09345) and AgentSight (arXiv 2508.02736).

## What's built

Cordon installs as four hooks — `SessionStart`/`PreToolUse`/`PostToolUse`/`SessionEnd`, or
whatever the target agent calls them — all routed through `cordon hook`. See [Setup](#setup)
for the per-agent install command.

When the first tool call fires, the hook starts one background sampler for the whole session
instead of spawning a fresh process per call. A process per call would add around 100ms of
startup overhead inside the same window it's trying to measure, which would contaminate the
result.

The sampler walks up from the hook process to find the agent's root process (`claude.exe` on
Windows), then polls memory and CPU across its entire process tree every 250ms, appending to
`samples.jsonl`. Each `PreToolUse` and `PostToolUse` event writes a timestamped marker to
`markers.jsonl`.

`cordon reduce` joins the two streams into one record per tool call: start and end time, peak
and average memory, average CPU, and the raw per-tick samples for that call's window. The raw
samples are kept rather than discarded after aggregation, since later analysis needs them to
look at burst shape, not just averages.

`cordon analyze` runs the reduced data through five characterization passes (execution time
split, peak-to-average memory ratio, per-tool breakdown, retry-loop detection, CPU/memory
correlation) plus two burst measures, then renders a report with a measured-versus-paper verdict
for each one.

Every hook path exits 0, no matter what happens internally. A broken measurement should never
break the agent it's measuring.

## Stage 2: control

Every guarded tool call gets its own ephemeral cgroup, named `tool_<pid>_<timestamp>`, created
before the subprocess spawns and torn down after it exits. The child joins the cgroup itself,
before `exec`, so allocations aren't missed in the gap between fork and the parent noticing.

The limits come from what the agent said it was about to do. Before a tool call it can set
`AGENT_RESOURCE_HINT=memory:high`, and that tier resolves to a `memory.high` soft limit and a
`cpu.weight` for that one call. Crossing `memory.high` throttles; it doesn't kill. Hints are
advisory — the agent can be wrong, and nothing here trusts one beyond setting a soft limit.

Feedback goes the other way too. If a call stalls past a threshold, or gets frozen, or gets
OOM-killed, Cordon appends a plain-English note to that call's stderr naming the peak, the limit,
and how long it stalled. The agent reads its own tool output on the next turn, so it sees that
note as part of the result and can narrow its scope or declare a higher tier, rather than just
failing and retrying the same thing.

What isn't built is the in-kernel policy layer. `sched_ext` (CPU) needs Linux 6.12+, and
`memcg_bpf_ops` (memory) is still an unmerged RFC patch series. Both let throttling decisions
happen in-kernel in microseconds instead of in a userspace daemon at tens of milliseconds, which
matters against bursts that last a second or two. Neither is stubbed or simulated — `cordon
control probe` reports what the machine actually has, and the code runs at whatever tier that
allows. `docs/stage2-design.md` covers what unblocks what.

## Install

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Setup

Claude Code, Codex CLI, Hermes Agent, Cursor CLI, and Gemini CLI each shipped their own hook
system, and every one of them is a renamed copy of the same idea: matcher + command groups,
JSON piped to a script on stdin carrying `tool_name`/`tool_input`/`session_id`/`cwd`, exit code
`2` (or a decision field) to block. `cordon hook` speaks that dialect for all five through one
small alias table (`features/wrapper/agents.py`) instead of a separate binary per agent. Aider
has no such hook system at all, so it gets a different command — see below.

Pick the section for whichever agent you're running.

### Claude Code

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo --write
```

Drop `--write` first if you want to see the `.claude\settings.json` merge before anything gets
touched. This is the default agent — `--agent claude-code` is implied if you omit it.

### Codex CLI

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo --agent codex --write
```

Writes `.codex\hooks.json` and patches `.codex\config.toml` with `[features]\ncodex_hooks =
true` — hooks are an opt-in, still-under-development Codex feature and are off by default.
Codex also won't run an unreviewed hook: run `/hooks` inside Codex once to trust it. Verify hooks
actually fire on your Codex version and platform before relying on the data; this is the newest
hook surface of the five and the most likely to have moved since this was written.

### Hermes Agent

```
.venv\Scripts\cordon.exe install-hooks --agent hermes --write
```

`--target` is ignored here: Hermes hooks live in `~/.hermes/config.yaml`, a user-global file,
not a per-repo one (set `CORDON_HERMES_HOME` to point somewhere else for testing). Same
one-time trust step as Codex: run `hermes hooks` to approve the freshly-registered shell hook,
unless `hooks_auto_accept: true` is already set in your config — Cordon does not set that for
you.

### Cursor CLI / Cursor Agent

If you already have Claude Code hooks installed, Cursor can load `.claude\settings.json`
directly instead of a separate config: enable *Settings → Rules, Skills, Subagents → Include
third-party Plugins, Skills, and other configs*, and the Claude Code install above is all you
need. Otherwise:

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo --agent cursor --write
```

writes Cursor's own `.cursor\hooks.json` shape.

### Gemini CLI

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo --agent gemini --write
```

Hooks are enabled by default (v0.26.0+). Google has announced Gemini CLI is being superseded by
Antigravity CLI for unpaid-tier and Google One users — check which one you're actually running
before pointing `--agent gemini` at it.

### Aider (and anything else without a hook system)

Aider has no `PreToolUse`/`PostToolUse`-shaped hook system — there's no moment between "the
agent decides to act" and "the action runs" to hook into. `cordon wrap` covers that case by
spawning the agent itself as a direct child and sampling its exact PID, giving you one
session-level peak/avg memory and CPU record instead of a per-tool-call breakdown:

```
.venv\Scripts\cordon.exe wrap -- aider --message "fix the failing test"
```

`cordon reduce` on a wrapped run works the same way and reports `n_toolcalls: 0` — that's
expected, not a bug — but `cordon analyze`'s per-tool breakdown and retry-loop passes need
paired tool-call markers, so they're not meaningful on wrap-only data.

## Measure

Run the agent normally. Cordon writes a sample stream and marker log per session under
`runs\<session-id>\`. Once the session ends, reduce it into one record per tool call:

```
.venv\Scripts\cordon.exe reduce --run-dir runs\<session-id>
```

Once you've reduced a batch of sessions, characterize the whole set:

```
.venv\Scripts\cordon.exe analyze --runs runs --out docs\stage1-findings.md
```

Pass `--json` instead of `--out` for the raw numbers rather than the rendered report.

## Use: control

Check what the machine can actually enforce:

```
.venv\Scripts\cordon.exe control probe
```

Run one command under a per-call cgroup, with a declared hint:

```
.venv\Scripts\cordon.exe control run --hint memory:high -- pytest tests/
```

The command's exit code passes through unchanged. On a machine without cgroup v2 the command
still runs; Cordon logs what it would have applied and enforces nothing.

Measure what the enforcement is worth under CPU contention:

```
.venv\Scripts\cordon.exe control contend --out docs\stage2-contention.md
```

## Layout

```
assets/              logo, banner, social preview — see docs/design-language.md
features/wrapper/   sampler, hook entrypoint, reducer, JSON-lines schema, agent registry, wrap
features/analysis/  characterization passes over reduced tool-call records
features/control/   capability probe, intent protocol, cgroup backends, guarded runner
docs/                design notes and findings
tests/                pytest suite
```

## Docs

- `docs/stage1-design.md` explains why interception happens through Claude Code's hooks
  instead of forking the agent, why sampling runs as one continuous process per session
  instead of one per call, and what that costs in measured overhead.
- `docs/stage1-findings.md` compares measured results against the papers' numbers. It's
  generated by `cordon analyze` and currently holds methodology and known structural
  differences; the results table fills in once a real task batch has been run.
- `docs/stage2-design.md` records which kernel features are available and which are blocking
  what, why the hint protocol uses tiers rather than absolute byte counts, what throttle
  threshold triggers feedback to the agent and why that number, and what was deliberately not
  built while the kernel side is gated.
- `docs/design-language.md` records the visual identity — palette, type, badge markup, and the
  GitHub topics/description to set for discoverability.

## License

MIT. See [LICENSE](LICENSE).
