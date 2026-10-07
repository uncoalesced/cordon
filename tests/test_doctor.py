# Engineered by uncoalesced

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from features import host
from features.wrapper import doctor
from features.wrapper.cli import _hook_command, main


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORDON_RUN_ROOT", str(tmp_path / "runs"))
    return tmp_path


def test_doctor_passes_on_this_machine(isolated: Path, capsys):
    assert main(["doctor", "--target", str(isolated)]) == 0
    out = capsys.readouterr().out
    for name in ("binary", "installed", "roundtrip", "agent_root", "sampler", "finalize", "run_root", "enforcement"):
        assert name in out
    assert "FAIL" not in out


def test_doctor_json_is_a_list_of_checks(isolated: Path, capsys, monkeypatch):
    monkeypatch.setattr(doctor, "check_sampler", lambda run_dir: doctor.CheckResult("sampler", doctor.PASS, "stub"))
    assert main(["doctor", "--json", "--target", str(isolated)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"check", "status", "detail", "fix"} <= set(rows[0])


def test_doctor_fails_with_a_fix_when_the_binary_is_missing(isolated: Path, capsys, monkeypatch):
    monkeypatch.setattr(host, "venv_script", lambda name, executable=None, os_name=None: isolated / "nope" / name)
    monkeypatch.setattr(doctor, "check_sampler", lambda run_dir: doctor.CheckResult("sampler", doctor.PASS, "stub"))
    assert main(["doctor", "--target", str(isolated)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  binary" in out and "fix: pip install -e ." in out
    # The interpreter fallback still round-trips, so only the binary check fails.
    assert "PASS  roundtrip" in out


@pytest.mark.skipif(os.name == "nt", reason="execute bits are POSIX-only")
def test_doctor_fails_when_the_binary_is_not_executable(tmp_path: Path, monkeypatch):
    script = tmp_path / "cordon"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o644)
    monkeypatch.setattr(host, "venv_script", lambda name, executable=None, os_name=None: script)
    result = doctor.check_binary()
    assert result.status == doctor.FAIL and result.fix.startswith("chmod +x")


def test_installed_check_finds_the_hook_after_install(tmp_path: Path):
    from features.wrapper.cli import install_hook_command

    command = install_hook_command("claude-code", "project")
    assert doctor.check_installed("claude-code", "project", str(tmp_path), command).status == doctor.WARN
    assert main(["install-hooks", "--target", str(tmp_path), "--write"]) == 0
    assert doctor.check_installed("claude-code", "project", str(tmp_path), command).status == doctor.PASS
    (tmp_path / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")
    assert doctor.check_installed("claude-code", "project", str(tmp_path), command).status == doctor.FAIL


def test_roundtrip_reports_a_failing_command(tmp_path: Path):
    result = doctor.check_roundtrip(f"{host.shell_quote(sys.executable)} -c \"raise SystemExit(3)\"", tmp_path)
    assert result.status == doctor.FAIL and "exit 3" in result.detail
    silent = doctor.check_roundtrip(f"{host.shell_quote(sys.executable)} -c \"pass\"", tmp_path)
    assert silent.status == doctor.FAIL and "no markers" in silent.detail


def test_a_crashing_check_is_reported_not_raised():
    def boom():
        raise RuntimeError("kaput")

    result = doctor._guarded("x", boom)
    assert result.status == doctor.FAIL and "kaput" in result.detail


def test_posix_hook_command_quotes_spaces_and_dollars():
    root = Path("/home/a b/$HOME/runs")
    command = _hook_command(root, os_name="posix")
    parts = shlex.split(command)
    assert parts[-2:] == ["--run-root", str(root)]
    assert parts[-3] == "hook"
    assert host.shell_quote("/a b/$x", os_name="posix") == "'/a b/$x'"


def test_is_executable_rejects_missing_files(tmp_path: Path):
    assert not host.is_executable(tmp_path / "missing")
    (tmp_path / "f").write_text("", encoding="utf-8")
    assert host.is_executable(tmp_path / "f", os_name="nt")
