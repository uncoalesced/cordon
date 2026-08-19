# Engineered by uncoalesced

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

import psutil

from features.wrapper.logging_setup import get_logger

ENV_HINT = "AGENT_RESOURCE_HINT"

TIER_ORDER = ("low", "medium", "high", "max")
DEFAULT_TIER = "medium"

MEMORY_TIER_FRACTION = {"low": 0.025, "medium": 0.10, "high": 0.35, "max": None}
MEMORY_TIER_FLOOR_MB = 256.0

CPU_TIER_WEIGHT = {"low": 25, "medium": 100, "high": 400, "max": 1000}
CPU_WEIGHT_RANGE = (1, 10000)

MEMORY_KEYS = ("memory", "mem", "ram")
CPU_KEYS = ("cpu",)

_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3}
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]*)$")
_SEPARATORS = re.compile(r"[,;\s]+")

_BYTES_PER_MB = 1024.0 * 1024.0

FEEDBACK_FLOOR_S = 0.2
FEEDBACK_FRACTION = 0.05


@dataclass
class Intent:
    memory_tier: str = DEFAULT_TIER
    cpu_tier: str = DEFAULT_TIER
    memory_high_bytes: int | None = None
    cpu_weight: int = CPU_TIER_WEIGHT[DEFAULT_TIER]
    source: str = "default"
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def memory_high_mb(self) -> float | None:
        if self.memory_high_bytes is None:
            return None
        return round(self.memory_high_bytes / _BYTES_PER_MB, 1)

    @property
    def memory_high_value(self) -> str:
        return "max" if self.memory_high_bytes is None else str(self.memory_high_bytes)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def total_memory_bytes() -> int:
    try:
        return int(psutil.virtual_memory().total)
    except Exception:
        get_logger("intent").warning("cannot read total memory, assuming 16GB for tier scaling")
        return 16 * 1024**3


def parse_size(text: str) -> int | None:
    match = _SIZE_RE.match(text.strip().lower())
    if match is None:
        return None
    amount, unit = match.groups()
    multiplier = _SIZE_UNITS.get(unit)
    if multiplier is None:
        return None
    return int(float(amount) * multiplier)


def memory_bytes_for_tier(tier: str, total_bytes: int | None = None) -> int | None:
    fraction = MEMORY_TIER_FRACTION.get(tier)
    if fraction is None:
        return None
    total = total_memory_bytes() if total_bytes is None else total_bytes
    floor = int(MEMORY_TIER_FLOOR_MB * _BYTES_PER_MB)
    return max(floor, int(total * fraction))


def cpu_weight_for_tier(tier: str) -> int:
    weight = CPU_TIER_WEIGHT.get(tier, CPU_TIER_WEIGHT[DEFAULT_TIER])
    return max(CPU_WEIGHT_RANGE[0], min(CPU_WEIGHT_RANGE[1], weight))


def resolve_intent(
    raw: str | None = None,
    env: Mapping[str, str] | None = None,
    total_bytes: int | None = None,
) -> Intent:
    log = get_logger("intent")
    env = os.environ if env is None else env

    source = "explicit"
    if raw is None:
        raw = env.get(ENV_HINT, "")
        source = "env" if raw else "default"

    intent = Intent(source=source, raw=raw or "")
    absolute_memory: int | None = None
    saw_memory = False

    for token in _SEPARATORS.split(raw or ""):
        if not token:
            continue
        key, _, value = token.partition(":")
        if not value:
            key, value = "memory", key

        key = key.strip().lower()
        value = value.strip().lower()

        if key in MEMORY_KEYS:
            if value in TIER_ORDER:
                intent.memory_tier = value
                saw_memory = True
                continue
            size = parse_size(value)
            if size is not None:
                absolute_memory = size
                intent.memory_tier = "absolute"
                saw_memory = True
                continue
        elif key in CPU_KEYS and value in TIER_ORDER:
            intent.cpu_tier = value
            continue

        message = f"ignoring unrecognised hint token {token!r}"
        log.warning("%s | %s=%r", message, ENV_HINT, raw)
        intent.warnings.append(message)

    if absolute_memory is not None:
        intent.memory_high_bytes = absolute_memory
    else:
        intent.memory_high_bytes = memory_bytes_for_tier(intent.memory_tier, total_bytes)

    intent.cpu_weight = cpu_weight_for_tier(intent.cpu_tier)

    if source == "env" and not saw_memory and not intent.warnings:
        log.info("hint carried no memory declaration, defaulting | %s=%r", ENV_HINT, raw)

    return intent


@dataclass
class FeedbackPolicy:
    floor_s: float = FEEDBACK_FLOOR_S
    fraction: float = FEEDBACK_FRACTION
    _counts: dict[str, int] = field(default_factory=dict)

    def threshold_s(self, duration_s: float) -> float:
        return max(self.floor_s, self.fraction * max(0.0, duration_s))

    def triggered(self, stall_s: float, duration_s: float, froze: bool, oom_kills: int) -> bool:
        if froze or oom_kills > 0:
            return True
        return stall_s >= self.threshold_s(duration_s)

    def evaluate(
        self,
        command: str,
        intent: Intent,
        stall_s: float,
        duration_s: float,
        peak_memory_mb: float,
        froze: bool = False,
        oom_kills: int = 0,
        observable: bool = True,
    ) -> str | None:
        if not observable:
            return None
        if not self.triggered(stall_s, duration_s, froze, oom_kills):
            return None

        occurrence = self._counts.get(command, 0) + 1
        self._counts[command] = occurrence
        return render_feedback(
            intent=intent,
            stall_s=stall_s,
            duration_s=duration_s,
            peak_memory_mb=peak_memory_mb,
            froze=froze,
            oom_kills=oom_kills,
            occurrence=occurrence,
        )


def render_feedback(
    intent: Intent,
    stall_s: float,
    duration_s: float,
    peak_memory_mb: float,
    froze: bool = False,
    oom_kills: int = 0,
    occurrence: int = 1,
) -> str:
    limit = intent.memory_high_mb
    limit_text = "no memory limit" if limit is None else f"a memory:{intent.memory_tier} limit of {limit} MB"
    share = (stall_s / duration_s * 100.0) if duration_s > 0 else 0.0

    parts = [
        f"[cordon] This tool call was resource-limited. It peaked at {peak_memory_mb:.1f} MB "
        f"against {limit_text}."
    ]
    if stall_s > 0:
        parts.append(
            f"It stalled {stall_s:.2f}s ({share:.0f}% of its {duration_s:.1f}s runtime) waiting on memory."
        )
    if froze:
        parts.append("It was frozen and resumed rather than killed, so no work was lost.")
    if oom_kills:
        parts.append(f"{oom_kills} process(es) in it were killed by the out-of-memory killer.")

    if occurrence >= 3:
        parts.append(
            f"This is limit warning number {occurrence} for this exact command; "
            "retrying it unchanged is unlikely to help."
        )

    parts.append(
        "Consider narrowing the scope of this command. If it genuinely needs more, "
        f"set {ENV_HINT}=memory:{_next_tier(intent.memory_tier)} before retrying."
    )
    return " ".join(parts)


def _next_tier(tier: str) -> str:
    if tier not in TIER_ORDER:
        return "high"
    index = TIER_ORDER.index(tier)
    return TIER_ORDER[min(index + 1, len(TIER_ORDER) - 1)]
