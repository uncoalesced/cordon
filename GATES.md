# Gates: Cordon v2 (branch feat/v2-platforms)

Plan: `~/.claude/plans/we-need-to-fix-sorted-quiche.md`. v1.1 gates archived in `gates/v1.1-GATES.md`.

Baseline 2026-10-27 (origin/main f30002c): 492 passed, 2 skipped, coverage 92%. Hook median 330 ms
(`scripts/bench_hook.py`, 20 Pre+Post pairs, Windows dev box, machine busy). ruff not run before.

- [x] G0 package renamed `features` -> `cordon`, dist `cordon-cli`, hatchling build, tests green
  CHECK: .venv\Scripts\python.exe -m pytest -q
  EVIDENCE: 2026-10-27 495 passed, 2 skipped, 92% (watermark test now covers cordon/ and scripts/)
- [x] G1 ruff check + format clean, lint job in CI
  CHECK: .venv\Scripts\ruff.exe check cordon tests scripts && ruff format --check cordon tests scripts
  EVIDENCE: "All checks passed!"; rule set E4/E7/E9/F/I/UP/B, B905 ignored
- [x] G2 no console windows from detached sampler/finalize on Windows
  CHECK: pytest tests/test_host.py -k console
  EVIDENCE: CREATE_NO_WINDOW replaces DETACHED_PROCESS; grandchild of popen_detached sees a hidden console (IsWindowVisible=0)
- [x] G3 one per-user daemon replaces per-session sampler spawns (no process per session start)
  CHECK: pytest tests/test_daemon.py; live: `cordon serve --ensure` x2 then GET /health
  EVIDENCE: 2026-10-27 live on Windows: health {"sessions":2}, one python pid, MainWindowHandle 0;
  40 Pre/Post POSTs + SessionEnd -> 43 markers, samples, report.md written in-process (no finalize spawn)
- [~] G4 hook median <= 80 ms
  EVIDENCE: Claude Code http transport: 3.3 ms median, p90 20 ms (40 loopback POSTs).
  ensure (once per session): 540 ms warm. Command transport (codex/gemini/cursor/hermes) still ~330 ms: open.
- [x] G5 markers.jsonl: 0 torn lines with 8 writers x 500 appends
  CHECK: pytest tests/test_attribution.py -k concurrent
  EVIDENCE: 2026-11-09 4000/4000 whole lines (cordon/lockfile.py: one os.write per line, O_APPEND,
  msvcrt/flock lock). Cause found in Parentheses ff5986a3:167: a command-transport `cordon hook`
  (hook_overhead 399 ms) appended through a buffered handle while the daemon wrote the same file.
- [x] G6 background training trees not charged to cat/nvidia-smi (re-reduce Parentheses 92a15925)
  EVIDENCE: 2026-11-09 re-reduced: every `cat` row 0-94 MB (its own shell); training trees listed
  under "Where the rest went" as background of the call that launched them.
- [x] G6b peer hook trees not charged to tool calls (positive attribution)
  CHECK: pytest tests/test_attribution.py
  EVIDENCE: 2026-11-09 Parentheses b769f654 before: Read 526 MB / Write 445 MB / "own 60-100
  procs" on every call. After: Read/Edit/Write/MCP = 0, top rows are the python runs (1774, 1312,
  732 MB). All 86 runs: hooks 24,787 CPU-s vs 10,860 CPU-s for every tool call; ecc's shared
  hook launcher alone spawned 413,320 processes in 34 sessions.
- [x] G7 runs without SessionEnd get finalized
  EVIDENCE: daemon finalizes on idle/agent exit (log 2026-11-04: "no markers for over 1800s" ->
  report written); every Parentheses run since 2026-10-27 has report.md. `finalize --stale` not needed.
- [x] G8 empty sessions labelled with SessionStart source
  EVIDENCE: 40 of 86 Parentheses runs are startup->end in ~2 s with 0 calls. summary.json `empty`,
  status hides them (--all shows), ALL-report leaves them out of figures; daemon now writes
  origin.json (agent + parent command line) at SessionStart so the next ones say who started them.
- [x] G8b failed calls paired: Claude Code PostToolUseFailure installed; old runs infer the end of
  a Bash/PowerShell call whose shell ran (exit_status "no end event"). Parentheses unpaired 989 -> 830,
  the rest are calls another PreToolUse hook blocked (Write 329, Edit 161) or denied.
- [ ] G9 Windows Job Object backend limits a 500 MB allocator at 200 MB (e2e in CI)
- [ ] G10 wheel builds, `uvx --from dist/*.whl cordon --version`, release workflow on tag
- [ ] G11 Claude Code plugin installs from local marketplace; report.md written; /cordon:report works
- [ ] G12 `cordon install --client all --dry-run` covers claude-code, codex, gemini, cursor, hermes, opencode, vscode
- [ ] G13 MCP server with code-mode query tool
- [ ] G14 Parentheses case study + sanitized example bundle, numbers regenerated
- [ ] G15 README restructured, privacy/terms, SECURITY/CONTRIBUTING, CHANGELOG current
- [ ] G16 all CI jobs green on PR
