# Engineered by uncoalesced
"""Which hook commands an agent session runs, so their processes are not billed to tool calls.

Claude Code spawns every command hook as ``<shell> -c <command>`` straight under the agent, at
the same instant it tells Cordon a tool is starting. Under the http transport those processes
look exactly like the tool's own. Knowing the configured commands lets reduce recognise them.

At SessionStart the daemon writes ``run_dir/hooks.json``: every command hook from the user,
project and local settings files, plus the hooks of each enabled plugin. Values stay raw
(``${CLAUDE_PLUGIN_ROOT}`` unexpanded) because that is what the spawned shell's command line
carries.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOOKS_SNAPSHOT_FILENAME = "hooks.json"

# The shell wrapper Claude Code puts around a hook command.
# The path may hold spaces (C:\Program Files\Git\bin\bash.exe), quoted or not.
_SHELL_WRAPPER = re.compile(
    r"^\s*\"?(?:[^\"\n]*?[\\/])?(?:bash|sh|zsh|cmd|powershell|pwsh)(?:\.exe)?\"?"
    r"\s+(?:-l\s+)?(?:-c|/d\s+/s\s+/c|/c|-Command)\s+",
    re.IGNORECASE,
)
# Fallback for runs recorded before hooks.json existed: a path through a hooks/ directory, or
# a `<tool> hook ...` subcommand (`rtk hook claude`).
HOOK_PATH_RE = re.compile(r"[\\/]hooks?[\\/]|(?<![\w/\\.-])hook\s", re.IGNORECASE)
_PLUGIN_RE = re.compile(r"plugins[\\/](?:cache|marketplaces)[\\/][^\\/]+[\\/]([^\\/\s\"']+)", re.IGNORECASE)
_HOOK_SCRIPT_RE = re.compile(r"[\\/]hooks?[\\/](?:[\w.-]+[\\/])*([\w.-]+?)(?:\.(?:js|mjs|cjs|ts|sh|py|ps1))?(?=[\"'\s]|$)")
_ANY_SCRIPT_RE = re.compile(r"([\w.-]+)\.(?:js|mjs|cjs|ts|sh|py|ps1)\b")
MIN_PREFIX = 8


def strip_shell(cmd: str) -> str:
    """The command a shell was asked to run (``bash.exe -c X`` -> ``X``); cmd itself otherwise."""
    match = _SHELL_WRAPPER.match(cmd)
    return cmd[match.end() :] if match else cmd


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _commands(hooks: Any, source: str) -> list[dict[str, str]]:
    """Command hooks out of a {"Event": [{"matcher", "hooks": [{"type", "command"}]}]} block."""
    found: list[dict[str, str]] = []
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            for hook in entries if isinstance(entries, list) else []:
                if isinstance(hook, dict) and hook.get("type", "command") == "command" and hook.get("command"):
                    found.append({"source": source, "event": str(event), "command": str(hook["command"])})
    return found


def _plugin_hooks(install_path: Path, name: str) -> list[dict[str, str]]:
    source = f"plugin {name}"
    found = _commands(_read_json(install_path / "hooks" / "hooks.json").get("hooks"), source)
    inline = _read_json(install_path / ".claude-plugin" / "plugin.json").get("hooks")
    if isinstance(inline, dict):
        found += _commands(inline.get("hooks", inline), source)
    elif isinstance(inline, str):
        found += _commands(_read_json(install_path / inline).get("hooks"), source)
    return found


def collect(cwd: Path | str | None, home: Path | None = None) -> list[dict[str, str]]:
    """Every command hook Claude Code would run for a session started in cwd."""
    claude = (Path.home() if home is None else Path(home)) / ".claude"
    settings_files = [("user settings", claude / "settings.json")]
    if cwd:
        project = Path(cwd) / ".claude"
        settings_files += [("project settings", project / "settings.json"), ("local settings", project / "settings.local.json")]

    found: list[dict[str, str]] = []
    enabled: dict[str, bool] = {}
    for source, path in settings_files:
        data = _read_json(path)
        found += _commands(data.get("hooks"), source)
        plugins = data.get("enabledPlugins")
        if isinstance(plugins, dict):
            enabled.update({str(k): bool(v) for k, v in plugins.items()})

    installed = _read_json(claude / "plugins" / "installed_plugins.json").get("plugins")
    if isinstance(installed, dict):
        for plugin_id, on in enabled.items():
            installs = installed.get(plugin_id) if on else None
            first = next((i for i in installs or [] if isinstance(i, dict) and i.get("installPath")), None)
            if first:
                found += _plugin_hooks(Path(first["installPath"]), plugin_id.split("@")[0])

    unique: dict[tuple[str, str], dict[str, str]] = {}
    for entry in found:
        unique.setdefault((entry["source"], entry["command"]), entry)
    return list(unique.values())


def write_snapshot(run_dir: Path, cwd: Path | str | None, home: Path | None = None) -> Path | None:
    """Record the session's hook commands once; a resumed session keeps the first snapshot."""
    path = Path(run_dir) / HOOKS_SNAPSHOT_FILENAME
    if path.exists():
        return path
    try:
        payload = {"cwd": str(cwd or ""), "hooks": collect(cwd, home)}
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        return None
    return path


def load_snapshot(run_dir: Path) -> list[dict[str, str]] | None:
    """The run's hook commands, or None when the run predates snapshots."""
    path = Path(run_dir) / HOOKS_SNAPSHOT_FILENAME
    if not path.exists():
        return None
    hooks = _read_json(path).get("hooks")
    return [h for h in hooks if isinstance(h, dict) and h.get("command")] if isinstance(hooks, list) else []


class HookMatcher:
    """Tells hook processes apart from everything else, and names them for the report."""

    def __init__(self, hooks: list[dict[str, str]] | None):
        # Process command lines are truncated (the sampler keeps 300 chars), so match prefixes.
        self._commands = [(h["command"].strip(), h) for h in hooks or [] if str(h.get("command", "")).strip()]

    def matches(self, cmd: str) -> list[dict[str, str]]:
        inner = strip_shell(cmd).strip()
        found = []
        for command, hook in self._commands:
            n = min(len(inner), len(command))
            if n >= MIN_PREFIX and inner[:n] == command[:n]:
                found.append(hook)
        return found

    def match(self, cmd: str) -> dict[str, str] | None:
        found = self.matches(cmd)
        return found[0] if found else None

    def is_hook(self, cmd: str) -> bool:
        return self.match(cmd) is not None or bool(HOOK_PATH_RE.search(strip_shell(cmd)))

    def label(self, cmd: str) -> str:
        """Short name for a hook process tree: its plugin and script where they can be read."""
        found = self.matches(cmd)
        if len({h["command"] for h in found}) > 1:
            # Truncated command lines of hooks sharing one launcher match several entries.
            sources = sorted({h["source"] for h in found})
            return f"{', '.join(sources)}: {len(found)} hooks sharing one launcher"
        hook = found[0] if found else None
        text = hook["command"] if hook else strip_shell(cmd)
        plugin = _PLUGIN_RE.search(text)
        source = hook["source"] if hook else (f"plugin {plugin.group(1)}" if plugin else "")
        script = _HOOK_SCRIPT_RE.search(text) or _ANY_SCRIPT_RE.search(text)
        if script:
            name = script.group(1)
        else:
            words = text.replace('"', " ").split()
            name = " ".join(words[:2])[:40] if words else "hook"
        if hook:
            name = f"{name} ({hook['event']})"
        return f"{source}: {name}" if source else name
