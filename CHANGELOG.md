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
