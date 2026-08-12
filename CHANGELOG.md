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
