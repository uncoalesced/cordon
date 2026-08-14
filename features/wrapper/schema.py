# Engineered by uncoalesced

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from features.wrapper.logging_setup import get_logger, log_failure

SCHEMA_VERSION = 1

EVENT_SESSION_START = "session_start"
EVENT_SESSION_END = "session_end"
EVENT_TOOL_START = "tool_start"
EVENT_TOOL_END = "tool_end"

MARKERS_FILENAME = "markers.jsonl"
SAMPLES_FILENAME = "samples.jsonl"
TOOLCALLS_FILENAME = "toolcalls.jsonl"
RUN_LOG_FILENAME = "cordon.log"


@dataclass
class Sample:
    t: float
    mem_mb: float
    cpu_pct: float
    n_procs: int = 0
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sample":
        return cls(
            t=float(raw["t"]),
            mem_mb=float(raw["mem_mb"]),
            cpu_pct=float(raw["cpu_pct"]),
            n_procs=int(raw.get("n_procs", 0)),
            partial=bool(raw.get("partial", False)),
        )


@dataclass
class Marker:
    event: str
    ts: float
    session_id: str = ""
    call_key: str = ""
    tool_type: str = ""
    command: str = ""
    cwd: str = ""
    exit_status: str = ""
    hook_overhead_ms: float = 0.0
    agent_pid: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Marker":
        return cls(
            event=str(raw["event"]),
            ts=float(raw["ts"]),
            session_id=str(raw.get("session_id", "")),
            call_key=str(raw.get("call_key", "")),
            tool_type=str(raw.get("tool_type", "")),
            command=str(raw.get("command", "")),
            cwd=str(raw.get("cwd", "")),
            exit_status=str(raw.get("exit_status", "")),
            hook_overhead_ms=float(raw.get("hook_overhead_ms", 0.0)),
            agent_pid=int(raw.get("agent_pid", 0)),
        )


@dataclass
class ToolCallRecord:
    task_id: str
    tool_type: str
    command: str
    start_ts: float
    end_ts: float
    peak_memory_mb: float
    avg_memory_mb: float
    avg_cpu_pct: float
    samples: list[dict[str, Any]] = field(default_factory=list)
    call_key: str = ""
    duration_s: float = 0.0
    n_samples: int = 0
    exit_status: str = ""
    hook_overhead_ms: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ToolCallRecord":
        return cls(
            task_id=str(raw.get("task_id", "")),
            tool_type=str(raw.get("tool_type", "")),
            command=str(raw.get("command", "")),
            start_ts=float(raw["start_ts"]),
            end_ts=float(raw["end_ts"]),
            peak_memory_mb=float(raw.get("peak_memory_mb", 0.0)),
            avg_memory_mb=float(raw.get("avg_memory_mb", 0.0)),
            avg_cpu_pct=float(raw.get("avg_cpu_pct", 0.0)),
            samples=list(raw.get("samples") or []),
            call_key=str(raw.get("call_key", "")),
            duration_s=float(raw.get("duration_s", 0.0)),
            n_samples=int(raw.get("n_samples", 0)),
            exit_status=str(raw.get("exit_status", "")),
            hook_overhead_ms=float(raw.get("hook_overhead_ms", 0.0)),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
        )


def call_key_for(session_id: str, tool_name: str, tool_input: Any, tool_use_id: str = "") -> str:
    if tool_use_id:
        return tool_use_id
    try:
        payload = json.dumps(tool_input, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        payload = repr(tool_input)
    digest = hashlib.sha1(f"{session_id}\x00{tool_name}\x00{payload}".encode("utf-8"))
    return digest.hexdigest()[:16]


def summarize_command(tool_name: str, tool_input: Any, limit: int = 2000) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "pattern", "path", "prompt", "url"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value[:limit]
        try:
            return json.dumps(tool_input, sort_keys=True, default=repr)[:limit]
        except (TypeError, ValueError):
            return repr(tool_input)[:limit]
    return str(tool_input)[:limit]


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        self._log = get_logger("jsonl")

    def write(self, obj: Any) -> bool:
        payload = obj.to_dict() if hasattr(obj, "to_dict") else obj
        try:
            self._handle.write(json.dumps(payload, default=repr) + "\n")
            self._handle.flush()
            return True
        except (OSError, TypeError, ValueError):
            log_failure(self._log, "jsonl write failed", path=str(self.path), payload=payload)
            return False

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            log_failure(self._log, "jsonl close failed", path=str(self.path))

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def append_jsonl(path: Path, obj: Any) -> bool:
    with JsonlWriter(path) as writer:
        return writer.write(obj)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    log = get_logger("jsonl")
    path = Path(path)
    if not path.exists():
        log.warning("jsonl file missing, yielding nothing | path=%s", path)
        return
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                log_failure(log, "skipping unparseable jsonl line", path=str(path), lineno=lineno, line=line[:200])
