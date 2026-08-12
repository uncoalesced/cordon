# Stage 1 — Characterization Design

Scope: measure what each agent tool call actually costs in CPU and memory. No enforcement,
no cgroups, no eBPF, no root. Runs on Windows, Linux and macOS.

Target of measurement: Claude Code itself (CLAUDE.md §10), on coding tasks.

## Interception: Claude Code hooks, not a fork

Claude Code fires `PreToolUse` before a tool runs and `PostToolUse` after it returns, passing
a JSON payload on stdin (`session_id`, `cwd`, `tool_name`, `tool_input`, plus `tool_response`
on the post side). Cordon registers a hook on both.

The alternative — patching Claude Code's tool-use loop directly — was rejected. It breaks on
every agent release, and it is the exact mistake AgentSight's boundary-tracing argument is
about (CLAUDE.md §7): instrument at a stable boundary the agent cannot bypass, not inside
framework internals that churn. The hook interface is that boundary.

Consequence: Cordon measures whatever agent honours this hook contract. Porting to another
framework means writing one new marker emitter, not rewriting the sampler or reducer.

## Sampling: one continuous sampler per session, not one per tool call

CLAUDE.md §5.2 sketches a sampler started and stopped around each tool call. Cordon instead
runs a single sampler for the whole session, and the hooks write cheap timestamped markers
that the reducer uses to slice the sample stream afterwards.

Why:

1. **Spawning a sampler inside `PreToolUse` contaminates the measurement.** Process startup on
   Windows costs roughly 100ms, and it lands inside the tool call's own measurement window —
   directly corrupting the numbers Stage 1 exists to produce.
2. **The idle stream is data, not waste.** The ~185MB framework baseline and the
   reasoning-versus-execution split (CLAUDE.md §11 analysis item 1) only exist if you sample
   between tool calls as well as during them.
3. **Burst attribution needs both.** The paper's finding that tool calls occupy 50.6% of
   sampling time yet contain 98.5% of bursts >300MB (§6) is computable only from a continuous
   stream partitioned by tool-call windows. A per-call sampler throws away the denominator.

The hooks still cost a process spawn each. That cost sits outside the sampled subprocess's own
work but inside the sampled process tree, so it is recorded rather than hidden: each reduced
record carries `hook_overhead_ms` for the pre-side marker write.

## Sampling interval: 250ms, not 1s

CLAUDE.md §11 says ~1s. The paper's own bursts last 1–2s and peak change rate reaches 3GB/s.
At 1s that is one or two samples per burst, and the recorded peak is whatever the sampler
happened to catch — aliasing that would make the burst-shape analysis meaningless.

250ms gives 4–8 samples per burst. Cost is 4× the sample volume, which for a 10-minute session
is a few MB. Configurable via `--interval`; 1s remains available for a direct comparison
against the paper's methodology.

## What gets sampled

The agent process and every descendant, summed. Tool calls run as forked subprocesses
(CLAUDE.md §5.1), so the parent alone shows nothing.

- **Memory:** RSS summed across the tree. Shared pages are double-counted where a parent and
  child share them; this is the same measure the paper reports, so the numbers stay comparable.
- **CPU:** `psutil` per-process CPU percent summed across the tree, so a fully busy 8-core
  machine reads as 800%, matching the paper's ">175% peak" convention.

Processes that exit mid-sample raise `NoSuchProcess`; those are dropped from that sample and
counted in `partial_samples` rather than aborting the sampler.

## Measured instrumentation overhead

Measured on the target Windows machine against a live Claude Code process tree of 10–11
processes:

| Metric | Value |
|---|---|
| `sample_once` median | 6.82 ms |
| `sample_once` p95 | 8.82 ms |
| `sample_once` max | 11.18 ms |
| Duty cycle of one core at 250ms | 2.73% |
| Duty cycle of one core at 1s | 0.68% |

Almost all of that is the process-table rescan in `children(recursive=True)`, and it scales with
total system process count rather than tree size. On an 8-core machine 2.73% of one core is
roughly 0.34% of total CPU — the same order as AgentSight's measured 2.9% end-to-end overhead
(CLAUDE.md §7), and small enough that it does not distort the burst measurements.

This number is re-measurable and should be re-checked on any machine before a characterization
batch, since it is the floor on how much Cordon perturbs what it measures.

## Output

Two append-only files per session under `runs/<session-id>/`:

- `markers.jsonl` — one line per hook firing (tool start, tool end, session lifecycle)
- `samples.jsonl` — one line per sampling tick

`cordon reduce` joins them into `toolcalls.jsonl`, one line per tool call in the CLAUDE.md §11
schema, raw per-tick samples included.

Three files rather than one because the two producers have different lifetimes and failure
modes. A crashed sampler must not lose the marker log, and a hook that fails to fire must not
corrupt the sample stream.

## Pairing starts to ends

Claude Code's hook payload does not reliably carry a tool-call identifier across versions.
Cordon prefers `tool_use_id` when present, and otherwise derives a key from
`session_id + tool_name + canonical(tool_input)`, pairing starts to ends LIFO per key.

This is exact for the common case and degrades on genuine concurrent duplicates — the same tool
invoked with byte-identical input twice at once. The reducer counts these in `unpaired_starts`
and `orphan_ends` rather than guessing, so contamination is visible in the output instead of
silently folded into the results.

## Error handling

Log and continue, everywhere. A failed sample, an unparseable marker, or a dead process
degrades that one record; it never ends a run. Failures are logged with full traceback and the
state that produced them, and counted in the run summary so a partially-degraded dataset is
identifiable as such rather than quietly wrong.
