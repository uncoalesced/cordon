# Stage 3 — Multi-Agent Interception Design

Stage 1 measures Claude Code. Nothing in the schema, sampler or reducer is Claude-Code-specific —
they operate on `session_id`, `tool_name`, `tool_input` and timestamps. Only `hook.py` knew which
agent it was talking to. Stage 3 moves that knowledge into per-target adapters so Cordon can
characterize any agent that fires a pre/post tool-call hook.

Audited and built 2026-08-14 against the four targets below. Cursor is deliberately excluded; see
the open question at the end.

## Why adapters, and why thin ones

Five agent frameworks have independently converged on close to the same hook contract: named
lifecycle events, a JSON payload describing the tool call, delivered before and after the tool
runs, with the pre-side able to block. That convergence is the whole opportunity — it means the
per-target work is translation, not reimplementation.

This is CLAUDE.md §7's boundary-tracing argument applied one level up. AgentSight's point is that
you instrument at a boundary the agent cannot bypass rather than inside framework internals that
churn. The hook interface is that boundary, and it turns out to be *almost the same boundary* on
every target. Writing a sampler, a reducer and a schema per agent would be rebuilding the parts
that already do not care which agent produced the data.

So: one core, five adapters, and an adapter is allowed to be four things and no more —

1. which native event names map to Cordon's four marker events,
2. how to pull `session_id` / `tool_name` / `tool_input` / `tool_use_id` out of that target's
   payload shape,
3. where that target wants its hook configuration written,
4. what that configuration looks like.

Everything else — marker writing, sampler spawn, call-key derivation, LIFO pairing, the
never-exit-non-zero guarantee — stays in `features/wrapper/` and is written once.

The payoff is measurable: of the five adapters, three need no custom extractor at all, because
their payload field names are identical to Claude Code's. Only Antigravity's differs enough to
need its own.

## Verification status per target

Cordon's whole value is that its numbers are trustworthy, so an adapter that has never seen a real
payload must not look like one that has. Each adapter carries a `verification` field, surfaced by
`cordon adapters`, with three possible values.

| Target | Verification | What was actually done |
|---|---|---|
| Claude Code | `live` | Stage 1's original path; hooks driven end to end by the existing suite |
| Claude Agent SDK | `live` | A real agent run on this machine, real credentials, real `PreToolUse`/`PostToolUse` captured |
| Codex CLI | `docs` | Event names, config locations and payload fields read from OpenAI's current primary docs; never run |
| VS Code agent mode | `docs` | Config locations and Claude-Code format compatibility read from Microsoft's current primary docs; never run |
| Antigravity | `docs` | Payload shape read from Google's current primary docs; never run, **and see the caveat below** |

`live` means a real payload from that target was observed. `docs` means the adapter was written
from the vendor's current primary documentation and has never processed a real event. There is no
third state that means "probably fine".

### Claude Agent SDK — verified live

Installed `claude-agent-sdk` 0.2.138, registered `PreToolUse` and `PostToolUse` hooks, ran a
minimal agent against real credentials, and captured this:

```json
{
  "session_id": "ef6e44fa-...", "transcript_path": "...", "cwd": "E:\\Cordon",
  "prompt_id": "f169feb2-...", "permission_mode": "bypassPermissions",
  "hook_event_name": "PreToolUse", "tool_name": "Bash",
  "tool_input": {"command": "echo cordon-probe", "description": "Echo cordon-probe string"},
  "tool_use_id": "toolu_013XPXQeg7gTS1QeRUYF8qKF"
}
```

`PostToolUse` added `tool_response` and `duration_ms: 2086`.

Every field Stage 1's hook already reads — `hook_event_name`, `session_id`, `cwd`, `tool_name`,
`tool_input`, `tool_use_id`, `tool_response` — is present and identically named. This is why the
SDK adapter was built first: it is the cheapest of the four and it validates the adapter pattern
before any effort is spent on targets that cannot be run.

Two things the live run showed that the SDK's own type stubs do not:

- The runtime payload carries `prompt_id` and, on the post side, `duration_ms`, neither of which
  appears in `PreToolUseHookInput` / `PostToolUseHookInput`. The declared types are not the whole
  truth, which is the argument for extracting permissively and ignoring unknown keys rather than
  validating against a fixed schema.
- `duration_ms` is the agent's own measurement of the tool call. Cordon computes duration from its
  marker timestamps, which include hook dispatch overhead on both sides. Where the target supplies
  its own figure it is recorded alongside, so the two can be compared rather than one silently
  standing in for the other.

**The SDK's hooks are in-process async callbacks, not subprocess-plus-stdin.** CLAUDE.md §12 says
sync only, and that still holds for everything Cordon does: the adapter's translation function is
sync and is the part with logic in it. The async wrapper is three lines at the edge, required by
the SDK's own callback signature, and does nothing but hand the payload to the sync path. Making
the core async to accommodate one target would be the tail wagging the dog.

The SDK also has **no `SessionStart` event** — its lifecycle events are `Stop`, `SubagentStart`,
`SubagentStop`, `PreCompact`, `Notification`, `PermissionRequest`, `UserPromptSubmit` and
`PostToolUseFailure`. Cordon already spawns the sampler on first `PreToolUse` as well as on
`SessionStart`, precisely so a target without a session-start event still gets sampled; `Stop` maps
to session end.

### Codex CLI — spec-only, installable but not run

`codex` is not on this machine's PATH. `@openai/codex` 0.147.0 installs from npm, so a live run is
possible in principle, but there are no Codex credentials here, and installing a CLI that cannot
authenticate would not have upgraded the verification status. Written from OpenAI's current docs.

Two findings that a summary from a previous session would have got wrong, which is why the primary
source was re-read:

- **Hooks are enabled by default**, not behind a feature flag. Earlier third-party write-ups
  describe an opt-in `[features].codex_hooks = true`; the current primary documentation says hooks
  ship on, and are disabled by setting `hooks = false` under `[features]`.
- **Not every tool fires the hook.** Local tools — Bash, MCP, `apply_patch`, local functions —
  trigger `PreToolUse`/`PostToolUse`. Hosted tools such as WebSearch do not, and the docs note
  that "some specialized tool paths can opt out of the default hook path".

That second point has a direct consequence for the numbers and is not a detail to bury: a Codex
characterization run measures the local-tool share of execution and is blind to hosted-tool time.
CLAUDE.md §6's Bash-dominance finding survives that blindness — Bash is a local tool — but any
"fraction of tool time" figure computed from Codex data has a denominator that excludes hosted
calls, and must be reported as such rather than compared naively against Claude Code's.

Field names match Claude Code's: `session_id`, `transcript_path`, `cwd`, `hook_event_name`,
`tool_name`, `tool_use_id`, `tool_input`, `tool_response`, plus Codex-specific `turn_id`, `model`
and `permission_mode`. The generic extractor handles it unchanged.

Config lives at `~/.codex/hooks.json`, `<repo>/.codex/hooks.json`, or inline `[[hooks.EventName]]`
tables in either `config.toml`. Cordon writes the repo-local `hooks.json`: it is JSON, so the
existing merge-and-refuse-on-unparseable logic applies directly, and writing TOML would mean adding
a TOML writer to a project whose only runtime dependency is `psutil`.

**To upgrade to `live`:** install `@openai/codex`, authenticate, register the hooks with
`cordon install-hooks --tool codex`, run one task, and confirm markers appear. The adapter should
need no change; if it does, that is the finding.

### VS Code agent mode — spec-only, and the closest to free

`code` 1.129.1 is present, but VS Code's agent mode is an interactive chat surface and the docs
describe no way to trigger a hook headlessly, so there was nothing to run.

The useful discovery is that VS Code reads Claude Code's configuration directly. Its documentation
states it "reads hook configurations from `.claude/settings.json`, `.claude/settings.local.json`,
and `~/.claude/settings.json` by default" and "parses Claude Code's hook configuration format,
including matcher syntax". Its events are Claude Code's: `SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `PreCompact`, `SubagentStart`, `SubagentStop`, `Stop`. Common payload
fields are `timestamp`, `cwd`, `session_id`, `hook_event_name`, `transcript_path`.

So the VS Code adapter is the Claude Code adapter with a different default config location —
`.github/hooks/cordon.json`, VS Code's own workspace path, chosen over `.claude/settings.json` so
that installing for VS Code does not silently also install for Claude Code in the same repo and
double-count a shared session.

One documented caveat is carried in the adapter rather than assumed away: VS Code notes
"differences in tool names and property casing between the platforms". Cordon's extractor reads a
small fixed set of keys and tolerates absence, so a casing difference degrades one field rather
than the record — but a Bash call named differently by VS Code would land in a different
`tool_type` bucket than Claude Code's, and per-tool-type comparisons across the two are only valid
after checking the bucket names actually agree.

**To upgrade to `live`:** run an agent-mode session in VS Code with the hooks installed and confirm
markers land. No API access needed — just a human driving the IDE once.

### Antigravity — spec-only, with a caveat larger than "untested"

Not installed, and no `antigravity` binary on PATH. Written from Google's current hook
documentation, which is specific: five events (`PreToolUse`, `PostToolUse`, `PreInvocation`,
`PostInvocation`, `Stop`), config in `hooks.json` under `.agents/` in the workspace or
`~/.gemini/config/`, and a payload shaped differently from every other target —

```
toolCall: {name, args}, stepIdx, error (post only),
conversationId, workspacePaths, transcriptPath, artifactDirectoryPath, modelName
```

camelCase, and the tool call nested under `toolCall` rather than flat `tool_name`/`tool_input`.
This is the one target that genuinely needs its own extractor, and it is the reason the extractor
is a per-adapter function rather than a shared parser with a field-name table.

**The caveat.** A controlled reproduction posted 2026-08-06 reports zero hook invocations in
Antigravity IDE 2.1.1 and Antigravity 2.0 desktop 2.5.0, with hooks registered through every
documented route — workspace, global and plugin — while the identical configuration fires on every
tool call in the `agy` CLI 1.1.10. The bundled offline docs describe hooks across the CLI, desktop
and IDE surfaces, but the changelog shows hook fixes landing only on the CLI track. Google has not
stated whether IDE hook execution is unimplemented, planned, or intentionally CLI-only.

So the honest position is not merely "we could not test this here". It is: **as of the audit date,
this adapter is expected to work against the `agy` CLI and is expected not to fire at all in the
Antigravity IDE**, and that is a property of Antigravity, not of Cordon. Anyone who installs the
Antigravity hooks and sees an empty marker log should check which surface they are running before
looking for a bug here.

**To upgrade to `live`:** install `agy` CLI 1.1.10 or later, install the hooks, run one task,
confirm markers. The IDE surface should be re-checked separately and independently.

### A follow-on, deliberately not built

Antigravity is the only target with `PreInvocation` / `PostInvocation` — events that bracket the
agent's *reasoning* step rather than a tool call. Cordon currently infers the reasoning-versus-tool
split by subtraction: tool windows come from markers, and everything else in the session stream is
treated as reasoning plus overhead. Those two events would let the split be measured directly, and
CLAUDE.md §6's "LLM reasoning is 26–44%, tool execution ~40%" is exactly the number that
measurement would sharpen.

Not built in this pass. It is new metrics work, not interception work, and it would need matching
changes in the reducer and the analysis passes to mean anything. Noted here so it is not lost.

## Extending `cordon install-hooks`

`install-hooks` gains `--tool`, defaulting to `claude-code` so the existing invocation keeps
working unchanged.

| Tool | Config written | Format |
|---|---|---|
| `claude-code` | `<target>/.claude/settings.json` | Claude Code hook settings |
| `vscode` | `<target>/.github/hooks/cordon.json` | same format; VS Code's own workspace path |
| `codex` | `<target>/.codex/hooks.json` | Codex hooks file |
| `antigravity` | `<target>/.agents/hooks.json` | named-hook object, `enabled` flag |
| `claude-agent-sdk` | — | not a file; the SDK takes hooks as Python objects |

The Agent SDK row is the interesting one. Its hooks are registered in code, so there is nothing to
merge into a settings file, and `install-hooks --tool claude-agent-sdk` prints the snippet to paste
into an agent script instead of writing anything. Pretending otherwise — inventing a config file
the SDK does not read — would be a worse lie than saying "this one is different".

What is explicitly *not* rebuilt per adapter: the dry-run-by-default behaviour, the merge into an
existing file rather than an overwrite, and the refusal to touch a file that will not parse. Those
live once in the CLI and take the adapter's path and payload as arguments. An adapter that could
independently decide to clobber a user's settings file is an adapter that will eventually do it.

Every target's config is JSON, which is what makes one merge routine sufficient. Codex also accepts
inline TOML tables and Antigravity's file is nested one level deeper under a hook name, but both
have a JSON form, and taking the JSON form everywhere keeps `psutil` the only runtime dependency.

## Payload handling

The extractor reads a small fixed set of keys and treats every one as optional. A payload missing
`tool_use_id` falls back to the derived call key from `docs/stage1-design.md`; a payload missing
`tool_input` yields an empty command string rather than an exception.

This is the same log-and-continue rule the rest of Cordon follows, and it matters more here than
elsewhere, because these payloads come from five vendors shipping on independent schedules. The
live SDK run already demonstrated the failure mode in miniature: the runtime payload contained two
fields the SDK's own published types do not declare. A strict parser would have been correct
according to the documentation and wrong according to the program. An unrecognised event name is
logged and ignored; an unparseable payload degrades that one marker.

## Open question — Cursor

**Cursor is not supported, and was not attempted.** Its hooks documentation describes
`beforeMCPExecution` and `afterMCPExecution`, which intercept *MCP tool* calls. Cordon needs to see
local tool calls — the Bash invocations that CLAUDE.md §6 found account for the overwhelming
majority of tool execution time, and the file reads and edits around them. Whether Cursor's hooks
fire for local bash and file tools, or only for MCP-routed ones, was not confirmed against a
primary source in this pass.

If they are MCP-only, a Cursor adapter would produce a marker log that silently omits nearly all of
the resource-relevant activity, and Cordon would report a Cursor session as almost entirely
reasoning time. That is a worse outcome than having no Cursor adapter: an obviously missing target
prompts a question, whereas a plausible-looking but structurally incomplete dataset gets used.

Resolving it needs one of: a primary-source statement that Cursor hooks cover local tool
execution, or an empirical check — install Cursor, register a hook, run a task that calls Bash and
edits a file, and see whether the hook fires. Until then, no adapter.
