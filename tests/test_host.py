# Engineered by uncoalesced

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from features import host
from features.wrapper import logging_setup


@pytest.mark.parametrize(
    "platform,expected",
    [("linux", host.LINUX), ("darwin", host.DARWIN), ("win32", host.WINDOWS), ("cygwin", host.WINDOWS), ("freebsd14", host.OTHER)],
)
def test_current_os(platform, expected):
    assert host.current_os(platform) == expected


def test_venv_script_per_os():
    assert host.venv_script("cordon", "/v/bin/python3", os_name="posix") == Path("/v/bin/cordon")
    assert host.venv_script("cordon", r"C:\v\Scripts\python.exe", os_name="nt").name == "cordon.exe"


def test_venv_script_default_is_this_interpreters_scripts_dir():
    import sysconfig

    assert host.venv_script("cordon").parent == Path(sysconfig.get_path("scripts"))


def test_shell_quote_survives_posix_metacharacters():
    assert host.shell_quote("/home/a b/$x`y`/cordon", os_name="posix") == "'/home/a b/$x`y`/cordon'"
    assert host.shell_quote(r"C:\a b\cordon.exe", os_name="nt") == r'"C:\a b\cordon.exe"'


def test_detach_kwargs_posix_starts_new_session():
    kwargs = host.detach_kwargs(os_name="posix")
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.skipif(host.OS != host.WINDOWS, reason="creationflags constants only exist on Windows")
def test_detach_kwargs_windows_detaches():
    assert host.detach_kwargs(os_name="nt")["creationflags"] & subprocess.DETACHED_PROCESS


def test_user_data_dir_per_os(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert host.user_data_dir(env=env, os_kind=host.LINUX) == tmp_path / ".local" / "share" / "cordon"
    assert host.user_data_dir(env={**env, "XDG_DATA_HOME": "/x"}, os_kind=host.LINUX) == Path("/x/cordon")
    assert host.user_data_dir(env=env, os_kind=host.DARWIN) == tmp_path / "Library" / "Application Support" / "cordon"
    assert host.user_data_dir(env={**env, "LOCALAPPDATA": "/l"}, os_kind=host.WINDOWS) == Path("/l/cordon")
    assert host.user_data_dir(env=env, os_kind=host.WINDOWS) == tmp_path / "AppData" / "Local" / "cordon"


def test_configure_retargets_to_a_new_log_file(tmp_path):
    # Regression: configure() used to be a one-shot, so the hook's per-run log was never written.
    logging_setup.reset_for_tests()
    logging_setup.get_logger("x").info("bootstrap to stderr")
    target = tmp_path / "run" / "cordon.log"
    logging_setup.configure(log_path=target, to_stderr=False)
    logging_setup.get_logger("x").info("lands in the run log")
    for handler in logging_setup.logging.getLogger(logging_setup.LOGGER_NAME).handlers:
        handler.flush()
    assert "lands in the run log" in target.read_text(encoding="utf-8")


def test_configure_without_path_keeps_existing_file_handler(tmp_path):
    target = tmp_path / "a.log"
    logging_setup.configure(log_path=target, to_stderr=False)
    logging_setup.configure(to_stderr=True)
    logging_setup.get_logger().info("still here")
    for handler in logging_setup.logging.getLogger(logging_setup.LOGGER_NAME).handlers:
        handler.flush()
    assert "still here" in target.read_text(encoding="utf-8")
