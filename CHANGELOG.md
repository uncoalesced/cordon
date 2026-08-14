# Changelog

File-level granularity: every commit gets a line naming the file it touched and why.

## Unreleased — Stage 1

### Scaffolding

- `.gitignore` — exclude venv, bytecode, coverage and pytest caches. `runs/` and loose
  `*.jsonl` are excluded because sample streams are run artifacts, not source; curated
  result sets are whitelisted under `docs/data/` so a reproducible dataset can still be committed.
- `pyproject.toml` — setuptools build, `psutil` as the only runtime dependency, `cordon`
  console script, and pytest configured with branch coverage over `features/` in native
  output format. Coverage is wired from the first commit rather than retrofitted.
- `README.md` — orientation and the install/run path for Stage 1.
- `CHANGELOG.md` — this file.
- `docs/stage1-design.md` — records the two interception decisions (Claude Code hooks over
  forking the agent; continuous sampler over per-call sampler) and the 250ms sampling
  deviation from CLAUDE.md §11, with the reasoning for each. Also carries the measured
  instrumentation overhead (6.82ms median per tick, 2.73% of one core at 250ms) so the
  floor on measurement perturbation is on record rather than assumed.

### Wrapper

- `features/__init__.py`, `features/wrapper/__init__.py`, `features/analysis/__init__.py` —
  package markers for the feature-organized layout.
- `features/wrapper/logging_setup.py` — single named logger, optional per-run file handler,
  and `log_failure` which always emits a full traceback plus JSON-rendered context. Exists
  before anything that could fail, so graceful degradation has somewhere to report to from
  the first commit rather than being retrofitted.
- `features/wrapper/schema.py` — the CLAUDE.md §11 record shape as dataclasses, plus marker
  and sample types and JSON-lines IO. `call_key_for` derives a stable tool-call identifier
  from `session_id + tool_name + canonical(tool_input)` when Claude Code does not supply a
  `tool_use_id`, which is what lets a `PreToolUse` be paired to its `PostToolUse`. `read_jsonl`
  logs and skips malformed lines instead of aborting, so one truncated write cannot cost a run.
- `features/wrapper/sampler.py` — process-tree sampler. `resolve_agent_root` walks the hook
  process's ancestors to find the agent (matching `node`/`claude` process names) since tool
  calls run as forked children and the parent alone shows nothing. Sums RSS and CPU percent
  across the tree; processes that exit mid-tick are dropped and counted in `partial_samples`
  rather than failing the sample. Drift-free tick scheduling so a slow tick does not compound.
- `features/wrapper/hook.py` — the Claude Code hook entrypoint. Translates `SessionStart`,
  `PreToolUse`, `PostToolUse` and `SessionEnd` payloads into markers, spawns the detached
  session sampler on first need, and stops it via a STOP file on session end. Every path is
  wrapped so the hook returns 0 unconditionally: a broken measurement must never break the
  agent being measured. Records its own `hook_overhead_ms` from real process create time, so
  the cost of instrumentation is in the data rather than hidden in it.
- `features/wrapper/reduce.py` — joins markers and samples into `toolcalls.jsonl` in the §11
  schema. Pairs starts to ends LIFO per call key, and counts `unpaired_starts`, `orphan_ends`
  and `empty_windows` in the run summary so a degraded dataset announces itself instead of
  silently skewing results. Reports tool time as an interval union, not a sum, so concurrent
  tool calls cannot push the fraction above 1.
- `features/wrapper/cli.py` — `cordon sample|reduce|install-hooks|hook`. `install-hooks`
  defaults to a dry run and merges into any existing `.claude/settings.json` rather than
  overwriting it, and refuses outright if the existing file is unparseable.

### Tests

- `tests/conftest.py` — logger reset between tests, a run directory fixture, and a real
  memory-allocating child process fixture for the sampler integration tests.
- `tests/test_schema.py` — call-key stability and collision behaviour, command summarization,
  JSON-lines roundtrip, and the malformed-line skip path.
- `tests/test_sampler.py` — measurement against the live process, child inclusion, every stop
  condition, interval pacing, and that a single failing tick is counted and survived.
- `tests/test_reduce.py` — LIFO pairing, orphan and unpaired counting, inclusive window
  slicing, empty-window counting, interval-union tool time, and idempotent re-reduction.
- `tests/test_hook.py` — marker emission per event, start/end key agreement, sampler spawn and
  stop lifecycle, and that neither bad stdin nor a raising handler can make the hook exit
  non-zero. Covers a full hooks-then-reduce cycle end to end.
- `tests/test_cli.py` — settings merge idempotency, dry-run safety, refusal to clobber a
  corrupt settings file, and the sample and reduce commands end to end.

### Analysis

- `features/wrapper/schema.py` — added `ToolCallRecord.from_dict` so reduced records can be
  read back. The reducer only ever wrote them; the analysis pass has to load them.
- `features/analysis/dataset.py` — discovers and loads reduced runs, accepting either a single
  run directory or a root containing many. Malformed records and an unreadable summary each
  degrade that one run rather than failing the batch, since a characterization run represents
  minutes of agent time that should not be thrown away over one bad line.
- `features/analysis/metrics.py` — the five passes from CLAUDE.md §11 step 5 (execution split,
  peak/avg ratios, per-tool-type breakdown, retry detection, CPU/memory correlation) plus the
  two burst measures from §6 that the retained per-tick samples make possible. Baseline memory
  is the 10th percentile of the session stream rather than the median of samples outside tool
  windows: the window-complement definition poisons its own baseline on exactly the bursty,
  tool-dominated runs the project exists to characterize. Correlation uses stdlib
  `statistics.correlation`, so no numpy or scipy dependency enters the tree for one number.
- `features/analysis/report.py` — renders the metrics as markdown with a measured-versus-paper
  verdict per row, and carries the methodology and known structural divergences as fixed text
  so a regenerated report never loses them. Distinguishes "measured zero" from "not
  measurable", so an empty dataset reports no data instead of claiming ten divergences it
  never measured.
- `features/wrapper/cli.py` — added `cordon analyze`, with `--out` to write the findings
  document and `--json` for the raw numbers.
- `tests/conftest.py` — added tool-call, sample and run factories shared by the analysis tests.
- `tests/test_analysis_dataset.py` — loading a real reduced run, skipping unreduced
  directories, and surviving malformed records and an unreadable summary.
- `tests/test_analysis_metrics.py` — every pass, including Bash classification across all
  categories, burst-resistant baselines, retry grouping and its strict-consecutive boundary,
  correlation sign and the zero-variance case, and that a raising pass degrades rather than
  propagates.
- `tests/test_analysis_report.py` — verdict direction, that an empty dataset reports no data
  rather than divergence, degraded-run flagging, and that every section renders.
- `tests/test_cli.py` — added coverage for `analyze` across report, file and JSON output.
- `docs/stage1-findings.md` — generated by `cordon analyze`. Currently the methodology and the
  four known structural divergences from the paper's setup; measured tables replace the
  placeholder section once a task batch has been run.
- `docs/stage1-design.md` — documents the analysis pass, the baseline-definition change and
  why the obvious first definition was wrong, and the retry-detection limitation.
- `README.md` — documents the `analyze` step.

### CI and housekeeping

- `.github/workflows/ci.yml` — tests across ubuntu and windows on Python 3.11, 3.12 and 3.13,
  with an 85% coverage gate applied in CI rather than in `pyproject.toml` so a local run of one
  test file does not fail on coverage. Both platforms are in the matrix deliberately: Stage 1 is
  developed on Windows and Stage 2 targets Linux, so a Linux regression must not go unnoticed
  until the kernel work starts. A separate CLI job drives the installed `cordon` console script
  end to end — sample a real process tree, feed real payloads through the `hook` stdin
  entrypoint, then reduce and analyze — because the test suite imports modules and therefore
  proves nothing about the packaged entry points.
- `tests/test_watermark.py` — asserts every source file carries the `# Engineered by
  uncoalesced` watermark required by CLAUDE.md §1. Written as a test rather than a CI-only
  script so it fails locally at the moment the file is added, not an hour later in a pipeline.
- `tests/test_sampler.py` — made the child-inclusion test poll until the spawned child's
  allocation lands. It asserted immediately after spawn, which passes on a fast local machine
  and flakes on a loaded CI runner: the process exists before its memory does.
- `CLAUDE.md` — wrote §15 Open Questions locally, listed in the table of contents since the
  first draft but absent from the body. Eleven questions across data collection, measurement
  validity, Stage 2 gating and protocol design, each naming what it blocks or which number it
  changes. The file itself is no longer version-controlled (see below), so this content lives
  on disk only.
- Removed `CLAUDE.md` from git history entirely via `git filter-repo`, on both `main` and
  `development` — the repo is public, and the spec (including the internship-provenance note
  in §10) had already been merged into `main`. Untracking it going forward would not have
  removed what was already published; every commit was rewritten to strip the file, and both
  branches force-pushed. `README.md`'s references to it were removed.
- `.gitignore` — ignore `CLAUDE.md` so it can't be re-added by accident, and the `.git-broken*`
  / `.git-oldswap*` salvage copies left behind by earlier repo repair attempts.

## Unreleased — Stage 3

### Multi-agent interception

- `docs/stage3-multi-agent-design.md` — why one core plus thin adapters rather than a pipeline per
  agent: five frameworks have converged on nearly the same hook contract, so the per-target work is
  translation, not reimplementation, and an adapter is allowed to be four things only (event map,
  field extraction, config location, config shape). Records the audit verdict per target with the
  real captured Agent SDK payload, the two Codex findings that a stale summary would have got wrong
  (hooks are on by default, not feature-flagged; hosted tools such as WebSearch never fire them, so
  a Codex tool-time fraction has a different denominator), the Antigravity IDE non-firing report,
  and exactly what would upgrade each spec-only adapter to verified. Names the Cursor open question
  rather than dropping it, and notes Antigravity's `PreInvocation`/`PostInvocation` as a follow-on
  that would let the reasoning-versus-tool split be measured instead of inferred — deliberately not
  built, since it is metrics work rather than interception work.
- `features/adapters/__init__.py` — the adapter registry and `get_adapter`, which is
  case-insensitive and names every known tool when it rejects one, because the first thing anyone
  types is the tool name they guessed.
- `features/adapters/base.py` — the shared contract. `NormalizedEvent` is the one shape the core
  consumes; `standard_extract` reads the common payload and treats every field as optional. Being
  permissive is not laziness here: the live SDK run returned two fields (`prompt_id`,
  `duration_ms`) that the SDK's own published types do not declare, so a strict parser would have
  been correct per the documentation and wrong per the program. Also holds the command-hook
  settings builder and the merge routine, so an adapter cannot independently decide how to touch a
  user's config file.
- `features/adapters/claude_code.py` — Stage 1's Claude-Code-specific knowledge, extracted from
  `hook.py` unchanged in behaviour. Still the default tool, so every existing invocation keeps
  working.
- `features/adapters/claude_agent_sdk.py` — verified against a real agent run on this machine.
  Field names are identical to Claude Code's, which is what made this the cheapest adapter and the
  right one to validate the pattern with. The SDK has no `SessionStart`, so the sampler starts on
  the first `PreToolUse` and `Stop` closes the session; `PostToolUseFailure` maps to tool-end so a
  failed call closes its window instead of leaving an unpaired start. `hook_matchers()` returns
  ready-made SDK objects, and the async callback is three lines at the edge that hand straight to
  the sync path — the core stays sync per CLAUDE.md §12 rather than going async for one target.
- `features/adapters/codex.py` — written from OpenAI's current docs, never run. Field names match
  Claude Code's so the standard extractor is reused. Carries the hosted-tool blind spot as a caveat
  the CLI prints on install, because a Codex dataset silently missing WebSearch time is the kind of
  gap that gets compared against Claude Code's numbers without anyone noticing.
- `features/adapters/antigravity.py` — the one target whose payload genuinely differs: camelCase,
  with the call nested under `toolCall` and the session under `conversationId`, so it needs its own
  extractor. Its reported `error` string is converted into the shared error shape inside the
  adapter rather than by teaching the core to read a bare string as failure, which would have
  changed how every other target's responses are interpreted. Carries the reproduction showing zero
  hook invocations in the IDE against working hooks in the `agy` CLI, so an empty marker log there
  is attributable to Antigravity rather than hunted for in Cordon.
- `features/adapters/vscode.py` — written from Microsoft's current docs, never run. VS Code reads
  Claude Code's hook format directly, so this is the Claude Code adapter with a different config
  path: `.github/hooks/cordon.json` rather than `.claude/settings.json`, deliberately, so installing
  for VS Code cannot silently also install for Claude Code in the same repo and double-count one
  session. Carries the documented tool-name and casing differences as a caveat.
- `features/wrapper/hook.py` — genericized. It no longer knows any tool's event names or field
  names; it normalizes through an adapter, then does the parts that were always tool-independent:
  run directory, sampler spawn, call-key derivation, marker write, and the unconditional zero exit.
  Sampler spawn is now keyed on the normalized event rather than a hardcoded event-name set, which
  is what lets a target with no session-start event still get sampled. `_exit_status` additionally
  recognises `interrupted`, which is how the SDK reports a cancelled call.
- `features/wrapper/schema.py` — `Marker` gained `adapter` and `reported_duration_ms`. The first
  makes a mixed dataset self-describing, since a run directory no longer implies which agent
  produced it. The second records the target's own duration where it offers one, alongside rather
  than instead of Cordon's marker-derived figure, which includes hook dispatch on both sides.
  Both default, so existing marker logs still read back.
- `features/wrapper/cli.py` — `install-hooks` gained `--tool`, defaulting to `claude-code`. The
  dry-run default, the merge-don't-overwrite behaviour and the refusal to touch an unparseable file
  stay in one place and take the adapter's path and payload as arguments. Installing an unverified
  adapter prints a loud banner naming it as such plus that target's caveat, so "unverified" cannot
  quietly become "shipped". Added `cordon adapters` to list every target with its verification
  status. The Agent SDK has no config file, so its install prints the snippet to paste into an
  agent script rather than inventing a file the SDK does not read.
- `pyproject.toml` — packaged `features.adapters`, and added an `sdk` extra carrying
  `claude-agent-sdk` and `anyio` so the live integration test can run without making the SDK a
  runtime dependency of a project whose only one is `psutil`.
- `README.md` — added the supported-tools table with verification status, the `--tool` flag, and
  `cordon adapters`.
- `tests/test_adapters_base.py` — the registry, tolerant lookup, the standard extractor against
  both a full and an empty payload, self-reported duration parsing, event mapping, merge
  idempotency, and two contract tests over every adapter at once: each must map both tool events
  and a session end, and each unverified one must carry a caveat explaining what that costs.
- `tests/test_adapters_claude_agent_sdk.py` — unit coverage against the payload literally captured
  from the live run, plus an integration test that starts a real agent, lets a real `PreToolUse`
  and `PostToolUse` fire, and asserts the markers land in Cordon's schema. It skips cleanly when no
  credentials are present rather than failing, so CI stays green without them.
- `tests/test_adapters_claude_code.py` — pins the pre-refactor behaviour: same events, same
  settings path, same pairing, now additionally tagged with the adapter name.
- `tests/test_adapters_codex.py` — spec payload normalization including the Codex-only `turn_id`
  and `model` fields, the full lifecycle map, settings path and shape, a paired pre/post cycle, and
  that the hosted-tool caveat is actually present rather than assumed.
- `tests/test_adapters_antigravity.py` — the camelCase flattening, workspace-path-to-cwd across
  list, string and empty forms, error-to-exit-status, a missing `toolCall` degrading to empty
  fields rather than raising, the named-hook settings object, and that merging preserves someone
  else's hook in the same file.
- `tests/test_adapters_vscode.py` — Claude-Code-shaped payload normalization, `Stop` closing the
  session, and specifically that installing for VS Code leaves no `.claude/` directory behind.
- `tests/test_cli.py` — per-tool install paths, the tool name reaching the written hook command,
  the unverified banner appearing for a spec-only tool and staying absent for a verified one, the
  SDK printing a snippet and writing nothing, per-tool idempotency and unparseable-file refusal,
  and the `adapters` listing in both renderings.
