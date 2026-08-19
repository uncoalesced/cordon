# Changelog

## v1.0.0 — Multi-agent

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
