# Stage 2 — Control Design

Stage 1 measures a tool call. Stage 2 acts on it: each intercepted call runs in its own
ephemeral cgroup, carrying limits derived from what the agent declared it was about to do, and
the agent gets told in plain English when those limits bit.

This document records the environment gating (what can and cannot run today), the decisions
CLAUDE.md §15 left open, and the choices that differ from the §5.3 sketch.

## Environment: what is gated, and by what

Probed on the development machine, 2026-08-14, with `cordon control probe` and by hand:

| Capability | Status here | Needed for |
|---|---|---|
| Linux | no — Windows 11 host | everything below |
| cgroup v2 (`cpu`, `memory`) | **yes, under WSL2** | ephemeral per-call cgroups, `memory.high`, `cpu.weight`, freeze, kill |
| PSI (`memory.pressure`) | yes | measuring how long a call actually stalled |
| `sched_ext` | **no** — WSL2 kernel is 6.6.114.1, needs 6.12+ | Stage 2a in-kernel CPU policy |
| `memcg_bpf_ops` | **no** — RFC PATCH bpf-next v3, 23 Jan 2026, unmerged | Stage 2b in-kernel throttle policy |

Two things follow.

**First, the BPF policy layer is genuinely blocked and is not being faked.** `sched_ext` needs a
6.12+ kernel built with `CONFIG_SCHED_CLASS_EXT`; the only Linux reachable from this machine is
the `docker-desktop` WSL2 utility VM on 6.6, which has neither the directory nor a package
manager to fix it with. `memcg_bpf_ops` is worse: it is still an RFC series against `bpf-next`,
so no stock kernel anywhere has it. Neither is stubbed, mocked, or simulated. `cordon control
probe` reports both as absent and says so.

**Second, most of the §5.3 mechanism does not actually depend on either of them.** `memory.high`,
`memory.max`, `cpu.weight`, `cgroup.freeze`, `cgroup.kill`, `memory.oom.group` and PSI stall
accounting are plain cgroup v2 interfaces, available on any modern Linux without a patch. What
`sched_ext` and `memcg_bpf_ops` buy is not the *mechanism* but the *policy speed*: deciding, in
kernel, in microseconds, how long to throttle and who to deprioritize. That is the §4
responsiveness argument, and it is the part that is blocked.

So `Cgroup2Backend` is written as real, complete code against the real interface, not as a stub
around a missing feature. On this machine it never loads — `select_backend()` falls to
`NullBackend`, which records what it would have applied and runs the command unchanged. On the
laptop, once installed, it is the same code path with nothing swapped.

Every file and semantic the backend depends on was verified by hand against the live cgroup v2
mount in WSL2 before the code was written: the `+cpu +memory` delegation chain, `memory.high`
and `cpu.weight` and `memory.oom.group` accepting writes in a freshly created child, a process
self-joining via `cgroup.procs`, `memory.peak` present and exceeding `memory.current`,
`memory.events` exposing `high` / `max` / `oom_kill`, `memory.pressure` carrying a `full ...
total=` field, and freeze / kill / rmdir all behaving. What is untested is only the Python
running *inside* such a mount, because that VM has no Python.

**What unblocks what.** Stage 2a needs a 6.12+ Linux with `sched_ext` — CLAUDE.md §8 option A
(finish the laptop reinstall) or option C (cloud VM). Stage 2b needs that plus either the RFC
landing (watch https://lwn.net/Articles/1055698/) or a self-built patched kernel. The
cgroup-v2-only tier needs nothing but a normal Linux install, and delivers per-call limits,
freeze, kill and stall accounting today.

## Layout: one `features/control/`, not `stage2_cpu/` and `stage2_mem/`

The CPU and memory halves share the same cgroup lifecycle, the same intent protocol, the same
feedback channel and the same probe. Splitting them into two packages would put one module in
each and duplicate the four they share. `features/control/` is still feature-organized, not
layered — it is the enforcement feature, the way `features/wrapper/` is the interception feature.

```
features/control/probe.py       kernel capability detection, enforcement tier
features/control/intent.py      the bidirectional protocol: hints up, feedback down
features/control/cgroup.py      backend interface, real cgroup v2 backend, null backend
features/control/guard.py       one guarded tool call, start to finish
features/control/contention.py  synthetic CPU contention, unguarded vs guarded
```

## Q10 — tiers or absolute values for `AGENT_RESOURCE_HINT`

**Decision: tiers are the protocol; absolute values are accepted as an escape hatch.**

`AGENT_RESOURCE_HINT=memory:high` is what the agent should emit. `memory:2G` also parses.

Three reasons tiers win as the thing an agent emits:

1. **An ordinal judgment is a judgment the agent can actually make.** "This test suite needs more
   than that file read did" is something an LLM knows. "This test suite needs 2GB" is a guess
   dressed as a number, and §6 measured exactly how bad that guess is: peak memory across tasks
   spans 197MB–4GB (CV=147%), the same task run three times took 402s / 222s / 259s on three
   different solution paths, and no LLM-observable proxy predicts peak memory at all
   (conversation rounds r<0.11, output tokens r=-0.14). An agent asked for an absolute number
   will produce a confidently wrong one.
2. **The agent does not know what machine it is on.** 400MB is the paper's LOW tier on their
   16GB test box and is meaningless on a 128GB one. Tiers are resolved to bytes *by the system*,
   as a fraction of installed memory, so the same hint means the same *intent* everywhere.
3. **Tiers let the policy be retuned without the agent changing.** If `high` turns out to be too
   generous, that is one constant in `intent.py`, not a retrained emission habit.

Absolute values are accepted anyway, because a caller that genuinely knows the number — a
benchmark harness replaying a recorded trace at a fixed limit, reproducing §6's experiment —
should not have to launder it through a tier. Such a hint reports `memory_tier="absolute"` so it
is visibly distinguishable in the logs from a tier the agent chose.

**The tier table**, as a fraction of installed RAM, floored at 256MB so a small machine cannot
wedge itself:

| Tier | Fraction | On 16GB | `cpu.weight` |
|---|---|---|---|
| `low` | 2.5% | 410 MB | 25 |
| `medium` (default) | 10% | 1.6 GB | 100 |
| `high` | 35% | 5.7 GB | 400 |
| `max` | unlimited | `memory.high=max` | 1000 |

`low` at 2.5% lands on 410MB on a 16GB box, deliberately close to the paper's own 400MB LOW arm
(§6), so its experiment can be reproduced without special-casing. `medium` is the default when no
hint is given, because an undeclared call should be neither privileged nor punished. `cpu.weight`
is the cgroup v2 default of 100 at `medium`, so a `medium` call competes exactly as it would
have unguarded.

Parsing is deliberately forgiving, because the emitter is a language model: `memory:`, `mem:`
and `ram:` are all accepted, a bare `high` is read as a memory tier, `,` / `;` / whitespace all
separate, and dimensions can be combined (`memory:high,cpu:low`). An unrecognised token is
logged, recorded in `intent.warnings`, and skipped — a typo in a hint must not fail a tool call.

Hints stay advisory. §5.4 is explicit that the agent can be wrong, and nothing here trusts the
hint beyond setting a soft limit: `memory.high` throttles, it does not kill.

## Q11 — what throttling triggers downward feedback

**Decision: report when the call's cumulative memory stall exceeds `max(200ms, 5% of its wall
time)`. Always report a freeze or an OOM kill regardless of stall.**

The failure modes are asymmetric and both are real. Too chatty and the agent learns the message
is noise and stops reading it — and it will get the chance, because §6 found retry groups in
85–97% of tasks, one running to 56 consecutive identical calls. Too quiet and the agent never
learns the policy exists, which makes the whole upward half of the protocol pointless.

**Why a 200ms floor.** §6's own enforcement numbers put individual allocation stalls in the tens
of milliseconds — HIGH-priority P95 allocation latency was 70.97ms unguarded, 50.14ms guarded.
A 200ms cumulative stall is therefore not one unlucky allocation, it is several, which is the
difference between noise and a pattern. It is also comfortably above Cordon's own measured
instrumentation cost (`docs/stage1-design.md`: 6.82ms median per sample tick), so the threshold
can never be tripped by the measurement itself.

**Why the 5% term as well.** A fixed threshold misfires at both ends of the duration range §6
measured. Sub-agent calls average ~100s; 200ms of stall in one of those is 0.2% and worth
nothing. Plain Bash calls run 4–6s; 200ms there is 3–5% and worth saying. Taking the larger of
the two keeps the message proportional to the call it is about.

**Why freeze and OOM always report.** Those are not degrees of slowness, they are state changes
the agent must know about — a freeze means its call was suspended, an OOM kill means work was
destroyed. §4's whole argument is that termination costs an agent its accumulated context, so
the one thing that must never happen silently is a kill.

**Repeats escalate rather than repeat.** `FeedbackPolicy` counts warnings per command string
across the session. The first two carry the normal message; from the third on it appends *"This
is limit warning number N for this exact command; retrying it unchanged is unlikely to help."*
Suppression was the obvious alternative and is worse: it hides information exactly when the
agent is in the retry loop §6 says it is probably in. Escalating says more, not less, and says
the thing the retry loop needs to hear.

The message names the observed peak, the limit it was measured against, the stall in seconds and
as a percentage of runtime, and the next tier up by name, so the agent has a concrete action
rather than a complaint:

> `[cordon]` This tool call was resource-limited. It peaked at 1842.0 MB against a memory:medium
> limit of 1638.4 MB. It stalled 1.50s (54% of its 2.8s runtime) waiting on memory. Consider
> narrowing the scope of this command. If it genuinely needs more, set
> `AGENT_RESOURCE_HINT=memory:high` before retrying.

Feedback is suppressed entirely when the backend could not observe the call — a null backend, or
a cgroup the process never joined. Telling an agent it was throttled when nothing was enforcing
anything is worse than saying nothing.

## What is deliberately not built

**No userspace freeze-escalation loop.** §5.3 escalates throttling to `cgroup.freeze` when
throttling alone is not enough. The backend exposes `freeze()` and `thaw()`, but nothing calls
them automatically, and that is on purpose. A userspace loop that polls pressure and decides when
to freeze *is* `oomd`, and §4's responsiveness argument is precisely that this loop is too slow:
tens of milliseconds per round trip against bursts that last 1–2 seconds and change at 3GB/s.
Hand-rolling the thing the paper says does not work, in order to have something to show before
the kernel arrives, would be building the wrong system. That decision belongs to
`get_high_delay_ms`, in kernel, in Stage 2b.

**No `memory.max`.** Only `memory.high` is set. A hard limit invokes the OOM killer, and §4's
termination-cost argument says an agent tool call is the wrong thing to kill. `memory.oom.group`
*is* set, so that if the system OOM killer does fire, it takes the whole call atomically rather
than leaving a half-killed process tree.

**No BPF programs, no `scx_flatcg` fork.** Nothing to attach them to on this machine, and
untestable code written against an unavailable kernel is not prep, it is guesswork.

## Mechanism notes

**Attachment happens in the child, before `exec`.** `run_guarded` passes `preexec_fn` on POSIX so
the forked child writes its own PID to `cgroup.procs` before the real command replaces it. The
alternative — spawn, then move the PID from the parent — loses every allocation between fork and
the parent's write, which on a burst measured at 3GB/s is exactly the part worth catching.
Membership is confirmed after spawn and re-checked each poll until the call exits; a call that
never joined is marked `attached: false` and gets no feedback.

**Stall time comes from PSI, not from a counter.** `memory.events` counts *how many times*
`memory.high` was crossed, which is not a duration. `memory.pressure`'s `full ... total=` field is
cumulative microseconds of full stall, and because each call gets a *fresh* cgroup, that
cumulative figure is already scoped to the call — no baseline subtraction, no delta bookkeeping.

**Peak memory prefers `memory.peak`, falls back to a running max.** `memory.peak` landed in 5.19;
on older kernels the poll loop keeps its own max of `memory.current`, which under-reads a burst
shorter than one poll interval. The kernels Stage 2 targets all have `memory.peak`.

**Child stderr is captured to a temp file, not a pipe.** The feedback message has to be appended
after the command exits, so stderr cannot simply be inherited. A pipe would deadlock on a command
that writes more than the pipe buffer while the parent is busy polling cgroup stats; a temp file
cannot. The cost is that stderr arrives at once at the end rather than streaming.

**Teardown always runs.** Cgroup removal happens on success, on failure, on timeout and on
`KeyboardInterrupt`. If the cgroup is still populated at teardown — a forked grandchild that
outlived its parent — `cgroup.kill` fires first, which handles processes forking mid-kill; only
then does `rmdir` retry. A cgroup that still cannot be removed is logged as leaked rather than
silently retried forever.

## The contention experiment

`cordon control contend` is the before/after harness for Stage 2a's definition of done. It runs
the same fixed-work CPU load twice: once with every worker unguarded in a shared cgroup (the
no-isolation arm, mirroring §6's baseline), once with each worker in its own sibling cgroup
carrying its tier's `cpu.weight`. It reports per-tier mean / p95 / max completion and survival
count for both arms, with the HIGH-tier p95 delta as the headline — the same shape as §6's
"P95 allocation latency 70.97ms → 50.14ms, 29% improvement".

Workers report their own elapsed time rather than being timed by the parent, so the number is not
quantized by the parent's poll interval. Each worker self-joins its cgroup via a path passed in
the environment, rather than through `preexec_fn`, because this harness spawns all workers before
waiting on any of them and `fork` plus Python-in-the-child is a hazard worth not introducing for
a benchmark. A calibration run of one worker alone is reported alongside, so the contention
factor is visible rather than assumed.

On a null backend both arms are literally the same experiment run twice, so `render()` says so
explicitly instead of printing a difference that is pure noise. **This harness has not yet
produced a real number: it needs a Linux host.**

## Error handling

Same rule as Stage 1: log and continue. A cgroup that cannot be created, limits that cannot be
written, a stat read that raises, a backend that explodes on setup — each degrades that call to
unguarded-but-still-running and is logged with a full traceback and context. The command the
agent asked for always runs. A resource controller that can prevent a tool call from executing at
all is worse than the problem it was built to solve.
