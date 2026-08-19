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

## Unreleased — Stage 2

### Control

- `docs/stage2-design.md` — the gating record and the two protocol decisions CLAUDE.md §15 left
  open. Names which kernel features are present on the development machine and which are not
  (`sched_ext` needs 6.12+, the reachable WSL2 kernel is 6.6.114.1; `memcg_bpf_ops` is RFC v3
  against bpf-next and unmerged), and separates the mechanism from the policy: `memory.high`,
  `cpu.weight`, freeze, kill and PSI stall accounting are plain cgroup v2 and need no patch,
  while only the microsecond in-kernel *decision* is blocked. Answers Q10 (tiers are the
  protocol, absolute values an escape hatch, with the tier table as a fraction of installed RAM)
  and Q11 (feedback fires above `max(200ms, 5% of wall time)` of stall, always on freeze or OOM),
  each with the §6 measurement the number comes from. Also records what was deliberately *not*
  built: no userspace freeze-escalation loop, because that loop is `oomd` and §4 is the argument
  that it is too slow.
- `features/control/__init__.py` — package marker. One control package rather than
  `stage2_cpu` / `stage2_mem`, because both halves share the cgroup lifecycle, the intent
  protocol, the feedback channel and the probe; splitting would put one module in each and
  duplicate four.
- `features/control/probe.py` — kernel capability detection, and the honesty layer for the whole
  stage. Checks cgroup v2 controllers, whether children can actually be created, PSI,
  `/sys/kernel/sched_ext/` (distinguishing "kernel too old" from "new enough but absent", since
  those have different fixes) and `memcg_bpf_ops` by looking for the struct_ops in BTF. Collapses
  the result into an enforcement tier of `none` / `cgroup2` / `bpf` so no other module has to
  guess what it is allowed to do. Every probe is individually wrapped: a raising check reports
  unavailable rather than taking the report down.
- `features/control/intent.py` — the bidirectional protocol from CLAUDE.md §5.4, both directions.
  Upward, `AGENT_RESOURCE_HINT` parses forgivingly because the emitter is a language model:
  `memory:`/`mem:`/`ram:`, a bare tier, mixed separators, and combined dimensions all work, and
  an unrecognised token is logged and skipped rather than failing a tool call. Tiers resolve to
  bytes as a fraction of installed RAM, so the same hint means the same intent on a 16GB laptop
  and a 128GB workstation, with `low` landing on 410MB at 16GB to match the paper's own LOW arm.
  Downward, `FeedbackPolicy` decides when a throttle is worth telling the agent about and renders
  the message. Repeat warnings for the same command escalate rather than suppress, because
  suppression hides information exactly during the retry loops §6 found in 85–97% of tasks.
- `features/control/cgroup.py` — the ephemeral per-call cgroup lifecycle behind two backends.
  `Cgroup2Backend` is complete real code against the real interface, not a stub: it delegates
  `+cpu +memory` down the tree, creates `tool_<pid>_<ts>/` per call, writes `memory.high`,
  `cpu.weight` and `memory.oom.group`, reads peak from `memory.peak` with a running-max fallback
  for pre-5.19 kernels, takes stall time from `memory.pressure`'s cumulative `full total=` (which
  needs no baseline subtraction because each call's cgroup is fresh), and on teardown fires
  `cgroup.kill` before `rmdir` so a grandchild that outlived its parent cannot leak the cgroup.
  Every file and semantic it depends on was verified by hand against a live cgroup v2 mount
  before the code was written. `NullBackend` records what would have been applied and enforces
  nothing, which is what runs where there is no cgroup v2. `memory.max` is deliberately never
  set — a hard limit invokes the OOM killer, and §4's termination-cost argument says a tool call
  is the wrong thing to kill.
- `features/control/guard.py` — one guarded tool call end to end: resolve intent, create and
  limit the cgroup, spawn with the child joining `cgroup.procs` in `preexec_fn` *before* `exec`
  (moving the PID from the parent after spawn would miss allocations in the gap, which against a
  3GB/s burst is the part worth catching), poll stats while it runs, then decide feedback and
  append it to the call's stderr. Child stderr goes to a temp file rather than a pipe, because
  the message has to be appended after exit and a pipe would deadlock on a command that outwrites
  the buffer while the parent is polling. Feedback is suppressed when the process never actually
  joined its cgroup: telling an agent it was throttled when nothing was enforcing is worse than
  silence. Every failure path degrades to unguarded-but-running — a resource controller that can
  stop a tool call from executing at all is worse than the problem it exists to solve.
- `features/control/contention.py` — the synthetic before/after harness for Stage 2a's definition
  of done. Runs identical fixed-work CPU load unguarded and then guarded, and reports per-tier
  mean/p95/max plus survival, with HIGH-tier p95 as the headline, mirroring §6's experiment
  shape. Workers time themselves and report on stdout so the number is not quantized by the
  parent's poll interval, and they self-join their cgroup through an environment variable rather
  than `preexec_fn`, since this harness has all workers in flight at once and `fork` with Python
  in the child is not a hazard worth taking for a benchmark. On a null backend both arms are the
  same experiment run twice, so the report says that outright instead of printing noise as if it
  were a result.
- `features/wrapper/cli.py` — added `cordon control probe|run|contend`. `probe` exits non-zero
  when nothing can be enforced, so it works as a gate in a script; `run` passes the guarded
  command's exit code straight through, so it is transparent to whatever invoked it.
- `pyproject.toml` — added `features.control` to the packaged modules.
- `README.md` — replaced the "Stage 2 hasn't been built" section with what is built and what
  is still gated, and documented the three `control` verbs.
- `tests/test_control_probe.py` — every capability check against synthetic filesystem shapes,
  including the too-old-kernel versus interface-absent distinction, the enforcement tier table,
  and that a raising probe degrades instead of propagating.
- `tests/test_control_intent.py` — hint spellings, combined dimensions, absolute sizes, tier
  scaling and monotonicity, the floor on small machines, and the full Q11 threshold: quiet below
  the floor, firing above it, the fractional term protecting long calls, freeze and OOM always
  reporting, escalation on repeats, and silence when the backend cannot observe.
- `tests/test_control_cgroup.py` — the real backend driven against a synthetic cgroup tree:
  controller delegation, limit writes, self-join, stat parsing across peak, pressure and events,
  the pre-5.19 peak fallback, freeze/thaw, and that teardown kills survivors before giving up.
  Missing stat files degrade to zero rather than raising, since a cgroup interface that varies by
  kernel version is the normal case, not an error.
- `tests/test_control_guard.py` — one cgroup per call and always torn down, hints flowing up into
  limits, feedback landing on stderr alongside the child's own output, no feedback when nothing
  was throttled or when the process never attached, exit codes passing through, timeouts, and
  that a backend raising on setup or on every stat read still lets the command run.
- `tests/test_control_contention.py` — both arms running, one cgroup per guarded worker, the
  baseline arm never touching the backend, a failing backend still running its worker, and that
  the report refuses to claim a result on a null backend.
- `tests/test_cli.py` — added coverage for the three `control` verbs, including exit-code
  passthrough, the recorded call log, and clean aborts when the guard or the experiment raises.

### HC8 feasibility audit

- `docs/stage2-hc8-audit.md` — the Step 0 audit of HC8, the phone-hosted host proposed for
  Stage 2's kernel work, with the raw command output rather than a summary. Verdict is NO-GO on
  all seven checks. Establishes what HC8 actually is: not proot-distro, not a chroot and not an
  Android VM, but Termux running natively as an unprivileged Android app under the
  `untrusted_app_27` SELinux domain, using Termux's Debian package format — which is why it looks
  like Debian from inside. Kernel is 4.14.356 against sched_ext's 6.12 floor, every capability set
  including the bounding set is zero, `/sys/fs/cgroup/` cannot even be listed, there is no kernel
  BTF, and the device is unrooted. Also records that `psutil` refuses to build on Android at all,
  so Stage 1 measurement cannot run there either. Names the five things that would have to change
  and why steps 2 and 3 of that list are a device port rather than a configuration.
- `features/control/probe.py` — fixed four ways the probe misreported reality, each found by
  running it on HC8 rather than reasoning about it. `kernel_release()` read
  `platform.uname().release`, which on Android CPython returns the Android release (`16`) instead
  of the kernel — a probe that reports 16 against a 6.12 threshold can talk itself into a GO on a
  4.14 kernel, so it now reads the `os.uname()` syscall, as does the new `kernel_sysname()`
  (`platform.system()` is `Android` there, which would have reported a Linux kernel as not Linux).
  `EACCES`/`EPERM` are now distinguished from `ENOENT` for the cgroup mount and for kernel BTF,
  because "not mounted" and "you will never be allowed to touch it" have completely different
  fixes. Added `probe_capabilities`, which reads the real bits from `/proc/self/status` and calls
  out an empty bounding set as permanent rather than merely unsatisfied — HC8's single most
  decisive signal, and the probe had been blind to it. Added environment identification so the
  platform line names Android, Termux and proot instead of reporting a bare kernel version.
- `features/control/probe.py` — the `bpf` enforcement tier now additionally requires the
  capability check to pass. `sched_ext` and `memcg_bpf_ops` being present is not enough if the
  process can never hold `CAP_BPF`.
- `tests/conftest.py` — made the `psutil` import lazy, inside the one fixture that uses it.
  It was at module scope, so on a host where psutil cannot be installed the entire suite failed
  to collect, including the capability-probe tests that have no need of it. Those tests now run
  on the target host, which is the only place their answers can be verified.
- `tests/test_control_probe.py` — coverage for all of the above: the Android kernel-release trap,
  an Android kernel still being reported as Linux, denied-versus-missing for both the cgroup mount
  and BTF, the capability bits including the empty-bounding-set case, environment naming for
  Termux and proot, and the tier table extended with the capability requirement.

### Stage 2 host audit — the two remaining devices

- `docs/stage2-host-audit.md` — the Step 0 audit of the two devices offered as the Stage 2 host,
  with raw command output rather than a summary. Device 1 turns out to be HC8 itself: same SSH
  endpoint, same uid and SELinux context, same 4.14 kernel build string, and Tailscale names it
  `headless-chicken-8-pro` and types it android. Re-audited anyway rather than assumed, and the
  probe table comes back identical line for line — still NO-GO, exit 1. Device 2 (`joel-folding`,
  a genuine Linux node per Tailscale) could not be audited at all: SSH refused the available key
  over the tailnet IP, over MagicDNS, and from device 1 as a jump host, and the MagicDNS attempt
  returning `publickey,password` proves Tailscale SSH is not enabled server-side. It is recorded
  as unaudited rather than stretched into a verdict, with the one `authorized_keys` line that
  unblocks it. Also dates CLAUDE.md §15 Q8: the `memcg_bpf_ops` series is still RFC v3 against
  bpf-next as of 2026-01-23 and the surrounding LWN coverage of 2026-05-15 still describes the
  area as early stage, so Stage 2b stays blocked on a self-built kernel and §13's definition of
  done for it is untouched.
- `features/wrapper/schema.py` — `DEFAULT_INTERVAL_S` moved here from `sampler.py`. It is a plain
  constant that the control layer and the CLI parser both need, and reading it from `sampler.py`
  dragged psutil into every importer. `sampler.py` re-exports it, so `sampler.DEFAULT_INTERVAL_S`
  and `hook.DEFAULT_INTERVAL_S` still resolve unchanged.
- `features/control/intent.py` — `import psutil` moved inside `total_memory_bytes()`, the one
  function that uses it. The existing `except Exception` already covered the failure, so a host
  without psutil now falls back to the documented 16GB tier-scaling assumption and logs it rather
  than failing at import.
- `features/control/guard.py` — takes `DEFAULT_INTERVAL_S` from `schema` instead of `sampler`,
  cutting the first of the two chains that reached psutil from the CLI.
- `features/wrapper/sampler.py` — re-exports `DEFAULT_INTERVAL_S` from `schema` rather than
  defining it. Nothing in the sampling loop changed, so Stage 1's measured overhead is unaffected.
- `features/wrapper/cli.py` — imports `sampler` and `hook` inside the two commands that need them.
  Together with the above this makes `cordon control probe` runnable through its own entry point on
  a host without psutil, which is the whole point of a probe: on device 1 it previously died with
  `ModuleNotFoundError: No module named 'psutil'` and printed no table. The HC8 pass fixed this
  same coupling in `tests/conftest.py` and left the sibling caller in `cli.py` untouched, so this
  finishes that fix at the pattern rather than at one symptom.
- `tests/test_cli.py` — runs `control probe` in a subprocess with psutil blocked at import and
  asserts a real table comes back. Verified to fail against the previous arrangement, so the
  regression is pinned rather than assumed.

### Visual identity

- `assets/banner.svg` — README header, 1200×320. Cordon tape — black ground, hazard-amber
  diagonal stripes — rather than the generic tech gradient, because the project's job literally is
  drawing a boundary around a subprocess and deciding what may cross it. The name picked the
  palette, not the other way round. Both hazard bands are one 28px `<pattern>` rotated 45°, so
  there is no per-stripe markup to maintain and no raster to keep in sync. The canvas is 320 rather
  than a rounder 300 because the bracket glyphs flanking the wordmark descend to y=169 against a
  tagline baseline at y=225; at 300 the two collided. The wordmark sits at `x="609"`, not 600:
  browsers add `letter-spacing` after the final glyph as well, and `text-anchor="middle"` centres
  on that full advance, so a word tracked at 18 and centred at 600 renders 9px left of true centre
  — which had driven the `[` 1.5px into the `C`. Brackets moved out to 320/880 for 17.5px of even
  clearance on both sides, verified against the real Arial metrics rather than by eye, and checked
  to hold whether or not a renderer counts the trailing tracking. Type is system stacks only
  (Arial, Consolas), so nothing depends on a webfont surviving GitHub's SVG sanitizer.
- `assets/mark.svg` — 200×200 square mark: an amber `C` on black ground with a hazard band across
  the lower third. Source for the repo avatar and any favicon; a PNG gets exported per surface
  rather than committing one raster per size. The band stops at y=176 instead of running to the
  bottom edge so the 28px rounded corners still read as corners at avatar sizes, and the glyph
  clears it by 26px. Same two-colour rule and same system font stack as the banner.
- `assets/social-preview.png` — 1280×640, the dimensions GitHub requires. Its own composition
  rather than a crop of the banner: the mark stacked over an unbracketed wordmark with the repo URL
  beneath, and the mark inverted to amber-ground so it still reads as a thumbnail in a feed, where
  the banner's 19px tagline would not. Committed as a PNG because GitHub's social-preview upload
  does not accept SVG. It cannot be applied from a commit — the upload is manual, and
  `docs/design-language.md` says where.
- `docs/design-language.md` — palette, type rules, badge markup and repo metadata, written down so
  the identity survives having to be re-derived later. Records why the palette is two colours plus
  one dim state: black and amber carry everything, and `cordon-amber-dim` exists only to mark
  something not-yet-complete — Stage 2's kernel-side layer, currently — without spending a third
  hue on it, the same way the codebase stays sync-only on purpose. Badges are pinned to
  `style=flat-square` because the rounded default and `for-the-badge` both read as a consumer
  product, which this isn't. Corrected against the assets as actually committed: the banner is
  1200×320 rather than 300, and there is no `social-preview.svg` to regenerate the preview from, so
  the instruction is to rebuild the composition at size rather than to re-export a source that does
  not exist. Also carries the two steps that cannot be done from a commit at all — the
  social-preview upload and the topics/description — so they do not get lost.
- `README.md` — banner and a seven-badge row above the existing prose. The badges are raw
  `<a>`/`<img>` HTML rather than the Markdown recorded in `docs/design-language.md`, because they
  sit inside a `<p align="center">` and Markdown image syntax cannot be centred on GitHub; all
  seven URLs are identical between the two files, and they are meant to be kept in sync by URL
  rather than by pasting the Markdown block over, which would silently drop the centring. The
  banner is displayed at `width="720"` against a 1200-wide source so it stays sharp on HiDPI.
  Committed last of the five, so every asset path it references already exists.

## v1.0.0 — Stage 3: multi-agent

### Multi-agent hook support

- `features/wrapper/agents.py` — per-agent hook config registry. Claude Code, Codex CLI,
  Hermes Agent, Cursor CLI, and Gemini CLI each shipped their own hook system, and every one of
  them turned out to be a renamed copy of the same idea: matcher + command groups, JSON on
  stdin carrying `tool_name`/`tool_input`/`session_id`/`cwd`, exit code 2 to block. Claude Code,
  Codex, and Gemini CLI share one JSON shape byte-for-byte (only the event names differ, so
  `NESTED_EVENTS` is a dict of four-tuples over one render function); Cursor's `hooks.json` is
  flat instead of matcher-grouped, so it gets its own small render/merge pair; Hermes's
  `~/.hermes/config.yaml` is YAML, global rather than per-repo, and additive-merged rather than
  overwritten so an existing `hooks_auto_accept` or unrelated hook doesn't get clobbered.
  `ensure_codex_feature_flag` is a deliberately narrow line-based TOML patch — not a real writer
  — that only understands a single `codex_hooks` key inside `[features]`; anything more exotic
  in a real `config.toml` (inline tables, arrays of tables) needs a TOML library instead.
- `features/wrapper/hook.py` — event-name aliasing generalized from a Claude-Code-only set to
  one alias set per canonical marker (start/pre/post/end), covering all five agents' spellings.
  Field extraction grew three fallbacks the single-agent version didn't need: session identity
  checks `conversation_id` after `session_id` (Cursor's tool hooks use the former, its lifecycle
  hooks the latter); tool-call identity checks `extra.tool_call_id` after `tool_use_id` (Hermes
  nests it); and exit-status extraction now handles a JSON-encoded string result (Cursor's
  `tool_output`), an `extra` dict carrying `status`/`error_type` (Hermes), and an `error` key
  (Gemini's `AfterTool`) in addition to the original `exit_code`/`is_error` shapes.
- `features/wrapper/sampler.py` — `AGENT_PROCESS_NAMES` extended with `hermes`, `codex`,
  `cursor-agent`, `gemini`, and `aider` (plus their `.exe` variants), so `resolve_agent_root`
  can find the right process tree to sample under each agent, not just Claude Code's `claude`.
- `features/wrapper/wrap.py` — new `cordon wrap` command for agents with no
  `PreToolUse`/`PostToolUse`-shaped hook system at all. Aider is the current example: there is
  no moment between "the agent decides to act" and "the action runs" to intercept, so the only
  thing left to measure is the whole invocation. `run_wrap` spawns the given command directly,
  which means it already has the exact child PID to sample — no process-name heuristic needed,
  unlike the hook-driven path. Produces the same `SessionStart`/`SessionEnd` marker pair `cordon
  reduce` already handles when a hook-based run happens to have zero paired tool calls, so
  nothing downstream needed to change to accept it; `cordon analyze`'s per-tool-call passes
  correctly find nothing to say about a wrap-only run rather than failing on one.
- `features/wrapper/cli.py` — `install-hooks` gained `--agent` (defaulting to `claude-code`,
  so the existing single-agent invocation still works unchanged) and dispatches to `agents.py`'s
  per-shape render/merge functions; Codex additionally gets its `config.toml` feature-flag patch
  written alongside `hooks.json`. New `wrap` subcommand mirrors `control run`'s argv-after-`--`
  handling.
- `pyproject.toml` — added `pyyaml` as a runtime dependency, for the Hermes `config.yaml`
  merge. Round-tripping arbitrary existing YAML by hand (regex-editing it, the way the Codex
  TOML flag is handled) was judged too fragile for a file that might already carry a user's own
  hooks and settings; a real YAML load/dump is the safer failure mode there. Version bumped to
  1.0.0.
- `tests/test_agents.py`, `tests/test_hook.py`, `tests/test_cli.py`, `tests/test_sampler.py` —
  coverage for the registry (per-agent settings paths, merge idempotency, the TOML patch's
  edge cases), the cross-agent event/field aliasing, `--agent` end-to-end installs for all five
  agents, and the new process names.
- `README.md` — restructured `## Use` into a `## Setup` section with one subsection per agent
  (Claude Code, Codex CLI, Hermes Agent, Cursor CLI, Gemini CLI, Aider), each with its exact
  install command and the trust/consent step it needs beyond that command where one exists.
