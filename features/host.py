# Engineered by uncoalesced

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import IO, Any

# Every place Cordon used to branch on os.name lives here, so Linux, macOS and Windows differ in
# exactly one file. Named "host" rather than "platform" so it never shadows the stdlib module.

LINUX = "linux"
DARWIN = "darwin"
WINDOWS = "windows"
OTHER = "other"


def current_os(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return LINUX
    if platform == "darwin":
        return DARWIN
    if platform in ("win32", "cygwin"):
        return WINDOWS
    return OTHER


OS = current_os()


def venv_script(name: str, executable: str | None = None, os_name: str | None = None) -> Path:
    """Console script installed for the running interpreter: bin/<name> or Scripts\\<name>.exe.

    By default asks sysconfig: a venv keeps scripts beside python, but a system/framework Python
    on Windows keeps them in a Scripts\\ subfolder. With an explicit executable, assume a venv.
    """
    windows = (os.name if os_name is None else os_name) == "nt"
    filename = f"{name}.exe" if windows else name
    if executable is None:
        return Path(sysconfig.get_path("scripts")) / filename
    return Path(executable).with_name(filename)


def is_executable(path: Path, os_name: str | None = None) -> bool:
    """A file the agent's shell can run: exists, and on POSIX carries an execute bit for us."""
    path = Path(path)
    if not path.is_file():
        return False
    return (os.name if os_name is None else os_name) == "nt" or os.access(path, os.X_OK)


def shell_quote(value: str, os_name: str | None = None) -> str:
    """Quote one argument for the shell an agent runs hook commands through (sh -c / cmd /c)."""
    if (os.name if os_name is None else os_name) == "nt":
        return f'"{value}"'
    return shlex.quote(value)


def detach_kwargs(stderr: IO[Any] | int | None = None, os_name: str | None = None) -> dict[str, Any]:
    """Popen kwargs for a background child that outlives the hook process that spawned it."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL if stderr is None else stderr,
    }
    if (os.name if os_name is None else os_name) == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # New session: no controlling terminal, so closing the agent's terminal (SIGHUP) or
        # Ctrl-C in it (SIGINT to the foreground group) never reaches the sampler.
        kwargs["start_new_session"] = True
    return kwargs


def user_data_dir(app: str = "cordon", env: dict[str, str] | None = None, os_kind: str | None = None) -> Path:
    """Per-user data dir: XDG on Linux, Application Support on macOS, LOCALAPPDATA on Windows."""
    env = dict(os.environ) if env is None else env
    os_kind = OS if os_kind is None else os_kind
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if os_kind == WINDOWS:
        base = env.get("LOCALAPPDATA")
        return Path(base) / app if base else home / "AppData" / "Local" / app
    if os_kind == DARWIN:
        return home / "Library" / "Application Support" / app
    base = env.get("XDG_DATA_HOME")
    return Path(base) / app if base else home / ".local" / "share" / app
