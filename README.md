<p align="center">
  <img src="assets/banner.svg" alt="Cordon — tool-call-granularity resource control for AI coding agents" width="720">
</p>


<p align="center">
  <a href="https://github.com/uncoalesced/cordon/actions/workflows/ci.yml"><img src="https://github.com/uncoalesced/cordon/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-f5c400?style=flat-square&labelColor=0d0d10" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-f5c400?style=flat-square&labelColor=0d0d10" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/measurement-cross--platform-f5c400?style=flat-square&labelColor=0d0d10" alt="Measurement: cross-platform">
  <img src="https://img.shields.io/badge/enforcement-cgroup%20v2-9a5b00?style=flat-square&labelColor=0d0d10" alt="Enforcement: cgroup v2, kernel policy layer pending">
  <a href="docs/stage1-design.md"><img src="https://img.shields.io/badge/grounded%20in-AgentCgroup%20%2F%20AgentSight-0d0d10?style=flat-square&labelColor=f5c400" alt="Grounded in AgentCgroup / AgentSight"></a>
</p>


Cordon watches what an AI coding agent does at the level of individual tool calls, not the
container as a whole. To most resource controllers, a `pytest` run and a `git status` both look
like "a subprocess." Cordon tells them apart, because one needs 500MB and the other needs 13MB,
and no single container-wide limit serves both well.

It measures first, then acts on what it measures. Measurement hooks into Claude Code, Codex CLI,
Hermes Agent, Cursor CLI, or Gemini CLI (Aider too, at a coarser grain) and tracks memory and CPU
per tool call on Linux, macOS, and Windows; when a session ends it writes a report for that
session automatically. Enforcement is a separate, explicit command: `cordon control run -- <cmd>`
runs one command under a limit sized from what the agent says it's about to do, and talks back
when the limit actually bites. **Installing the hooks does not enforce anything** — the hooks only
measure. How hard `control run` can enforce depends on the OS:

| Host | Backend | What it does |
|---|---|---|
| Linux, cgroup v2, normal user | `cgroup2` (delegated subtree under `user@UID.service`) | real `memory.high` throttle + `cpu.weight`, PSI stall time |
| Linux, root or writable cgroupfs | `cgroup2` (mount root) | same |
| Linux, systemd user manager only | `systemd-run` (`--user --scope`) | same limits, applied by systemd |
| Linux without usable cgroups (WSL1, proot, locked containers) | `advisory` | `nice` for CPU, user-space memory watchdog that warns, never kills |
| macOS | `advisory` | `nice` + `taskpolicy -b` for CPU, memory watchdog (unique set size) |
| Windows | `null` | runs the command unchanged, records what it would have applied |

The part that would react at kernel speed instead of userspace speed is still waiting on kernel
features that don't exist outside an RFC yet, which is stated plainly below rather than glossed
over.

Grounded in [AgentCgroup](https://arxiv.org/abs/2602.09345) and
[AgentSight](https://arxiv.org/abs/2508.02736).

## How Cordon works

Claude Code, Codex CLI, Gemini CLI, Cursor, and Hermes each fire a `PreToolUse`-and-`PostToolUse`
pair of events around every tool call, with a JSON payload on stdin (`session_id`, `cwd`,
`tool_name`, `tool_input`, and `tool_response` on the post side). Cordon registers `cordon hook`
against all of them and normalizes the differences through one alias table
(`features/wrapper/agents.py`) instead of shipping five separate integrations. This was a
deliberate choice over patching each framework's tool-use loop directly, since that breaks on
every release and instruments internals that churn. Hooks are a stable boundary the agent can't
route around.

On the first hook firing, Cordon starts one background sampler for the whole session rather than
spawning a fresh process per call, and lets the hooks write cheap timestamped markers that get
joined against the sample stream afterward. Spawning a process inside `PreToolUse` costs roughly
100ms on Windows, landing directly inside the window being measured, and the idle time between
calls is data too: the framework's baseline memory and the reasoning-versus-execution split both
depend on sampling between calls, not just during them. The sampler walks up from the hook's own
process to find the agent's root — scoring ancestors so that `node .../@anthropic-ai/claude-code/cli.js`
beats an unrelated `node` MCP server sitting in between (override with `CORDON_AGENT_PID`) — then
polls memory and CPU (per-process percent, summed across the whole process tree) every 250ms, since
the bursts this is meant to catch last 1-2 seconds and can change at multiple gigabytes per second.

Summed RSS double-counts pages shared between processes (every `node` child maps the same V8 and
libc pages), so each sample also carries `mem_mb_unique`, refreshed once a second: PSS from
`/proc/<pid>/smaps_rollup` on Linux, unique set size on macOS, private bytes on Windows. Reports
use it when present and say so. On Linux, if the agent already runs alone in its own cgroup (a
`systemd-run` scope, a terminal's per-app scope), the kernel's exact `memory.current` is recorded
as `cg_mem_mb` too. On the reference dev
machine, one sampling tick runs a 6.82ms median against a live 10-11 process Claude Code tree,
roughly 2.73% of one core. Re-measure this on your own machine before trusting a batch; it's the
floor on how much Cordon disturbs what it's watching.

`cordon reduce` joins markers to samples into `toolcalls.jsonl`, one record per tool call with
start/end time, peak and average memory, average CPU, and the raw per-tick samples (kept raw
because later analysis needs to see how a burst is shaped, not just how big it got). Not every
agent's hook payload carries a stable tool-call ID, so pairing falls back to
`session_id + tool_name + canonical(tool_input)`, matched last-in-first-out; the rare case of two
byte-identical concurrent calls gets counted in `unpaired_starts`/`orphan_ends` rather than
guessed at silently.

`cordon analyze` runs the reduced data through five passes (execution-time split,
peak-to-average memory ratio, per-tool breakdown, retry-loop detection, CPU/memory correlation)
plus two burst measures, and renders a report with a measured-versus-paper verdict for each one.
Two judgment calls worth knowing about: baseline memory is the 10th percentile of the session's
samples rather than the median of non-tool-call samples, since a session dominated by bursty
calls would otherwise poison its own baseline. And a retry group is three or more strictly
consecutive identical calls, matching the source paper's definition, which undercounts the
common pattern of a failing `pytest` alternating with a `Read`/`Edit` in between (`retry_profile`
takes an `ignore_tools` argument to relax this).

Every hook path exits `0` no matter what happens internally, and every sampling or analysis
failure is logged and skipped rather than raised. A broken measurement must never break the agent
being measured.

### Acting on what it finds

Enforcement runs one guarded command inside a single ephemeral cgroup (`tool_<pid>_<timestamp>`),
created right before the subprocess spawns and torn down right after it exits. Limits come from
what the agent says it's about to do: setting `AGENT_RESOURCE_HINT=memory:high` before a call
resolves to a `memory.high` soft limit and a `cpu.weight` for that call alone.

| Tier | Fraction of RAM | On 16GB | `cpu.weight` |
|---|---|---|---|
| `low` | 2.5% | 410 MB | 25 |
| `medium` (default) | 10% | 1.6 GB | 100 |
| `high` | 35% | 5.7 GB | 400 |
| `max` | unlimited | — | 1000 |

Hints are advisory, never trusted blindly: crossing `memory.high` throttles under pressure, it
doesn't kill. Only `memory.high` gets set, never `memory.max`, because an OOM kill destroys
whatever context the agent had already built up. A call whose cumulative memory stall (read from
PSI, not an event counter) exceeds `max(200ms, 5% of that call's wall time)` gets a plain-language
note appended to its stderr once it exits:

> `[cordon]` This tool call was resource-limited. It peaked at 1842.0 MB against a memory:medium
> limit of 1638.4 MB. It stalled 1.50s (54% of its 2.8s runtime) waiting on memory. Consider
> narrowing the scope of this command. If it genuinely needs more, set
> `AGENT_RESOURCE_HINT=memory:high` before retrying.

A freeze or OOM kill is always reported regardless of threshold. Repeats escalate rather than
repeat verbatim: from the third warning on the same exact command, the message notes that
retrying it unchanged is unlikely to help.

Run `cordon control probe` first to see what your machine can actually do: the capability bits,
cgroup v2 at the root or delegated to your user, `systemd-run --user`, PSI accounting, `sched_ext`,
and on macOS whether `taskpolicy` is there. It picks the strongest backend that passes a real
write test, not just a "controllers listed" check. On the advisory backend the stall line in the
warning reads "over its memory limit for N s" instead, because without cgroups there is no kernel
stall accounting to read; that number is labelled `stall_source: watchdog` in the JSON so it never
gets mixed up with PSI.

The cgroup v2 interfaces above are all ordinary Linux, available without a patch. What's missing
is the layer that would move the throttle *decision* into the kernel itself, microseconds instead
of a userspace loop's tens of milliseconds, which is what actually matters against a burst that
lasts a second or two. That needs `sched_ext` (Linux 6.12+) for CPU policy and a not-yet-upstream
`memcg_bpf_ops` RFC for memory policy. Neither is stubbed or faked; `cordon control probe` reports
both as absent on a machine that lacks them. There's also no automatic freeze-escalation loop
above the throttle on purpose, since a userspace loop that polls pressure and decides when to
freeze is just a slower rebuild of `oomd`, the exact thing this design tries to avoid.

## Install

Python 3.11+. Two ways:

**Global, recommended** — one `cordon` on your PATH, works on every OS, and avoids the
"externally-managed-environment" (PEP 668) error on Debian/Ubuntu and Homebrew Python:

```bash
pipx install git+https://github.com/uncoalesced/cordon
```

**From a checkout** (for hacking on Cordon; runs land in the repo's `runs/`):

Linux / macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/cordon doctor
```

Windows (PowerShell):

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\cordon.exe doctor
```

The examples below say `cordon`; from a checkout that means `.venv/bin/cordon` or
`.venv\Scripts\cordon.exe`.

`cordon doctor` answers "is this actually working?": it checks the binary is executable, the hook
is installed for your agent, the exact hook command round-trips through the shell your agent uses
(`sh -c` or `cmd /c`) and how long it takes, that the agent root resolves, that a sampler starts
and stops, that a report gets written, and what enforcement tier the machine has. Every failure
comes with the command that fixes it.

## Setup

Claude Code, Codex CLI, Hermes Agent, Cursor CLI, and Gemini CLI each shipped their own hook
system, and all five are a renamed copy of the same idea: matcher-and-command groups, JSON on
stdin, exit code `2` (or a decision field) to block. `cordon hook` speaks all five dialects
through one alias table. Aider has no hook system at all, so it gets a different command. Install
hooks for whichever agents you actually run; there's no need to set up all five.

### Claude Code

```bash
cordon install-hooks --target path/to/task-repo --write   # this repo only
cordon install-hooks --scope user --write                 # every repo: ~/.claude/settings.json
```

Default agent, `--agent claude-code` is implied. Drop `--write` first to preview the merged
settings file before anything on disk changes. `--scope user` also bakes `--run-root` into the
hook command, so runs land in your per-user data dir instead of whichever repo you're in:
`~/.local/share/cordon/runs` (Linux, honours `XDG_DATA_HOME`),
`~/Library/Application Support/cordon/runs` (macOS), `%LOCALAPPDATA%\cordon\runs` (Windows).
On Linux/macOS the hook command is single-quoted for `sh -c`, so install paths with spaces or `$`
work.

### Codex CLI

```bash
cordon install-hooks --target path/to/task-repo --agent codex --write
```

Writes `.codex/hooks.json` and adds `codex_hooks = true` to `.codex/config.toml`, since hooks are
still opt-in there. Run `/hooks` inside Codex once to trust the newly registered hook. This is the
newest hook surface of the five, so confirm it actually fires on your version before trusting the
data.

### Hermes Agent

```bash
cordon install-hooks --agent hermes --write
```

No `--target` needed: Hermes hooks live in `~/.hermes/config.yaml`, a user-global file (set
`CORDON_HERMES_HOME` to point elsewhere). Run `hermes hooks` once to trust the registered hook,
unless `hooks_auto_accept: true` is already set.

### Cursor CLI / Cursor Agent

If you've already installed Claude Code hooks, Cursor can load that same `.claude/settings.json`
directly: enable *Settings → Rules, Skills, Subagents → Include third-party Plugins, Skills, and
other configs*. Otherwise:

```bash
cordon install-hooks --target path/to/task-repo --agent cursor --write
```

### Gemini CLI

```bash
cordon install-hooks --target path/to/task-repo --agent gemini --write
```

Hooks are on by default from v0.26.0 onward. Google has said Gemini CLI is being superseded by
Antigravity CLI for unpaid-tier and Google One users, so confirm which one you're running.

### Aider (and anything else without a hook system)

Aider has no `PreToolUse`/`PostToolUse`-shaped hook system, so `cordon wrap` spawns the agent
itself as a direct child and samples that PID for the whole run, giving one session-level
peak/average record instead of a per-tool-call breakdown:

```bash
cordon wrap -- aider --message "fix the failing test"
```

`cordon reduce` reports `n_toolcalls: 0` on a wrapped run; that's expected. `cordon analyze`'s
per-tool and retry-loop passes need paired markers, so treat wrap-only data as session-level only.

## Measure

Run the agent normally with hooks installed. Cordon writes a marker log and sample stream per
session under `runs/<session-id>/`, and when the agent ends a session (or a turn — Claude Code's
`Stop`) a detached `cordon finalize` reduces it and writes `runs/<session-id>/report.md`. Nothing
to run by hand:

```bash
cordon report --last          # newest session's report
cordon report --session <id>  # a specific one
cordon report --all           # one report across every session
cordon status                 # every run: duration, tool calls, peak MB, report?, sampler live?
cordon status --clean         # stop samplers whose agent is gone or idle
```

The lower-level steps are still there for batch work:

```bash
cordon reduce --run-dir runs/<session-id>
cordon analyze --runs runs --out docs/stage1-findings.md   # --json for raw numbers
```

A sampler stops on session end, when the agent process exits, or after
`CORDON_IDLE_STOP_S` seconds (default 1800) with no new markers — so a crashed agent that never
sent `SessionEnd` doesn't leave one running. Per-run `cordon.log`, `sampler.stderr` and
`finalize.stderr` hold anything that went wrong.

## Control

```bash
cordon control probe
cordon control run --hint memory:high -- pytest tests/
cordon control contend --out docs/stage2-contention.md
```

`probe` reports what the machine can enforce. `run` guards one command with the strongest
backend available (table at the top) and passes its exit code through unchanged; with no backend
it still runs the command, just unguarded. On a systemd distro where cgroup v2 isn't delegated to
your user, `systemctl --user` usually is, and `run` uses a transient `systemd-run --user --scope`
for you. `contend` measures what enforcement is worth under synthetic CPU
contention, the same unguarded-vs-guarded shape as the source paper's own evaluation.

## Layout

```
assets/              logo, banner, social preview — see docs/design-language.md
features/wrapper/   sampler, hook entrypoint, reducer, JSON-lines schema, agent registry, wrap
features/analysis/  characterization passes over reduced tool-call records
features/control/   capability probe, intent protocol, cgroup/systemd-run/advisory backends, guarded runner
features/host.py     the one place Linux, macOS and Windows differ (paths, quoting, detaching, data dirs)
scripts/             e2e_control.py, the real-OS enforcement check CI runs on Linux and macOS
docs/                design notes and findings
tests/                pytest suite
```

## License

MIT. See [LICENSE](LICENSE).
