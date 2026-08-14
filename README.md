# Cordon

Cordon watches what an AI coding agent does at the level of individual tool calls, not the
container as a whole. To most resource controllers, a `pytest` run and a `git status` are both
just "a subprocess." Cordon tells them apart, because one needs 500MB and the other needs 13MB,
and a single container-wide limit can't serve both well.

Right now it only measures. Stage 1 hooks into Claude Code, tracks memory and CPU per tool call,
and turns that into a report you can compare against published numbers. Stage 2, which would
enforce limits based on what Stage 1 finds, hasn't been built yet.

Grounded in AgentCgroup (arXiv 2602.09345) and AgentSight (arXiv 2508.02736).

## What's built

Cordon installs as four Claude Code hooks: `SessionStart`, `PreToolUse`, `PostToolUse`, and
`SessionEnd`, all routed through `cordon hook`.

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

## What Stage 2 will do

Stage 1 only watches. Stage 2 is where Cordon would start acting on what it sees, and it's
designed but not built yet.

Each tool call would get its own ephemeral cgroup, created right before the subprocess spawns
and torn down after it exits. On the CPU side (Stage 2a), a `sched_ext` policy would decide
scheduling priority in-kernel, at microsecond speed. That matters because the alternative, a
userspace daemon watching pressure signals and reacting, takes tens of milliseconds per round
trip. A memory burst that lasts a second or two is often over by the time a daemon-based
approach would even notice it.

On the memory side (Stage 2b), each cgroup would get a `memory.high` soft limit. Crossing it
doesn't kill anything by default; it triggers reclaim pressure. A `memcg_bpf_ops` hook would
decide how long to throttle a call that crosses its limit, and only escalate to freezing the
process, not killing it, if throttling alone isn't enough. Killing a tool call mid-run destroys
whatever context the agent had built up to that point, and a retried agent doesn't reliably
reach the same solution twice, so a kill is treated as a last resort rather than the default
response to pressure.

The other half of Stage 2 is a feedback loop, not just a limit. An agent could set an
environment variable before a tool call, something like `AGENT_RESOURCE_HINT=memory:high`
before running a test suite, to hint at what it's about to need. That hint is advisory, not
binding; the system doesn't have to trust it. Going the other way, if a call gets throttled or
frozen past some threshold, Cordon would write a plain-English note to that call's stderr, for
example that it peaked at 3.5GB and got throttled for 340ms. Since the agent reads its own tool
output on the next turn, it would see that note as part of the result and could adjust rather
than just failing silently.

Stage 2a needs a Linux 6.12+ machine, which is where `sched_ext` shipped. Stage 2b needs more:
`memcg_bpf_ops` is a kernel patch series, not yet merged upstream, so it either needs a
self-built patched kernel or waiting for the RFC to land.

## Which agents it works with

The hook contract Cordon intercepts at is close to identical across agent tools, so the sampler,
reducer, schema and analysis are shared and only a thin adapter differs per target.

```
.venv\Scripts\cordon.exe adapters
```

| Tool | `--tool` | Verified | Config written |
|---|---|---|---|
| Claude Code | `claude-code` (default) | live | `.claude/settings.json` |
| Claude Agent SDK | `claude-agent-sdk` | live | none — registered in code |
| Codex CLI | `codex` | from docs | `.codex/hooks.json` |
| Antigravity | `antigravity` | from docs | `.agents/hooks.json` |
| VS Code agent mode | `vscode` | from docs | `.github/hooks/cordon.json` |

"Verified live" means a real payload from that tool was captured and is what the adapter's tests
run against. "From docs" means the adapter was written from the vendor's current documentation and
has never seen a real event — `install-hooks` prints a loud banner when you install one of those.
Cursor is not supported; see `docs/stage3-multi-agent-design.md` for why.

## Install

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Use

Point Cordon at the repo where the agent will actually work:

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo --write
```

Drop `--write` first if you want to see the settings.json merge before anything gets touched.

For a tool other than Claude Code, pass `--tool`:

```
.venv\Scripts\cordon.exe install-hooks --target C:\path\to\task-repo --tool codex --write
```

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

## Layout

```
features/wrapper/   sampler, hook entrypoint, reducer, JSON-lines schema
features/adapters/  per-agent hook translation into the shared marker schema
features/analysis/  characterization passes over reduced tool-call records
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
- `docs/stage3-multi-agent-design.md` covers the shared-core-plus-thin-adapter split, what was
  verified against a real payload versus written from vendor docs, what would upgrade each
  spec-only adapter, and why there is no Cursor support.

## License

MIT. See [LICENSE](LICENSE).
