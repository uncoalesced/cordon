# Engineered by uncoalesced

from __future__ import annotations

import pytest

from features.control.intent import (
    CPU_TIER_WEIGHT,
    ENV_HINT,
    MEMORY_TIER_FLOOR_MB,
    FeedbackPolicy,
    Intent,
    memory_bytes_for_tier,
    parse_size,
    render_feedback,
    resolve_intent,
)

GB = 1024**3
SIXTEEN_GB = 16 * GB


def test_no_hint_yields_the_medium_default():
    intent = resolve_intent(env={}, total_bytes=SIXTEEN_GB)
    assert intent.source == "default"
    assert intent.memory_tier == "medium"
    assert intent.cpu_weight == CPU_TIER_WEIGHT["medium"]


def test_env_hint_is_picked_up_and_marked_as_such():
    intent = resolve_intent(env={ENV_HINT: "memory:high"}, total_bytes=SIXTEEN_GB)
    assert intent.source == "env"
    assert intent.memory_tier == "high"
    assert intent.memory_high_bytes == int(SIXTEEN_GB * 0.35)


def test_explicit_hint_beats_the_environment():
    intent = resolve_intent("memory:low", env={ENV_HINT: "memory:max"}, total_bytes=SIXTEEN_GB)
    assert intent.source == "explicit"
    assert intent.memory_tier == "low"


@pytest.mark.parametrize("raw", ["memory:high", "mem:high", "ram:high", "high"])
def test_memory_tier_spellings_all_land_on_the_same_intent(raw: str):
    assert resolve_intent(raw, env={}, total_bytes=SIXTEEN_GB).memory_tier == "high"


def test_both_dimensions_can_be_declared_at_once():
    intent = resolve_intent("memory:high, cpu:low", env={}, total_bytes=SIXTEEN_GB)
    assert intent.memory_tier == "high"
    assert intent.cpu_tier == "low"
    assert intent.cpu_weight == CPU_TIER_WEIGHT["low"]


def test_max_tier_means_no_memory_ceiling():
    intent = resolve_intent("memory:max", env={}, total_bytes=SIXTEEN_GB)
    assert intent.memory_high_bytes is None
    assert intent.memory_high_value == "max"
    assert intent.memory_high_mb is None


@pytest.mark.parametrize(
    "text,expected",
    [("2g", 2 * GB), ("2G", 2 * GB), ("512M", 512 * 1024**2), ("1024k", 1024 * 1024), ("4096", 4096)],
)
def test_absolute_sizes_parse(text: str, expected: int):
    assert parse_size(text) == expected


def test_absolute_hint_is_accepted_as_the_escape_hatch():
    intent = resolve_intent("memory:2G", env={}, total_bytes=SIXTEEN_GB)
    assert intent.memory_tier == "absolute"
    assert intent.memory_high_bytes == 2 * GB


def test_unrecognised_tokens_are_warned_about_not_fatal():
    intent = resolve_intent("memory:enormous cpu:high", env={}, total_bytes=SIXTEEN_GB)
    assert intent.cpu_tier == "high"
    assert intent.memory_tier == "medium"
    assert intent.warnings


def test_tiers_scale_with_the_machine_but_never_below_the_floor():
    small = memory_bytes_for_tier("low", total_bytes=1 * GB)
    large = memory_bytes_for_tier("low", total_bytes=128 * GB)
    assert small == int(MEMORY_TIER_FLOOR_MB * 1024 * 1024)
    assert large > small


def test_tiers_are_monotonic():
    sizes = [memory_bytes_for_tier(t, SIXTEEN_GB) for t in ("low", "medium", "high")]
    assert sizes == sorted(sizes)


def test_feedback_stays_quiet_below_the_floor():
    policy = FeedbackPolicy()
    assert policy.evaluate("pytest", Intent(), stall_s=0.05, duration_s=2.0, peak_memory_mb=100.0) is None


def test_feedback_fires_once_the_floor_is_crossed():
    policy = FeedbackPolicy()
    message = policy.evaluate("pytest", Intent(), stall_s=0.5, duration_s=2.0, peak_memory_mb=1800.0)
    assert message is not None
    assert "1800.0 MB" in message
    assert ENV_HINT in message


def test_the_fractional_term_protects_long_calls_from_trivial_stalls():
    policy = FeedbackPolicy()
    assert policy.threshold_s(100.0) == pytest.approx(5.0)
    assert policy.evaluate("subagent", Intent(), stall_s=0.3, duration_s=100.0, peak_memory_mb=500.0) is None


def test_freeze_and_oom_always_report_regardless_of_stall():
    policy = FeedbackPolicy()
    assert policy.evaluate("a", Intent(), stall_s=0.0, duration_s=1.0, peak_memory_mb=1.0, froze=True)
    assert policy.evaluate("b", Intent(), stall_s=0.0, duration_s=1.0, peak_memory_mb=1.0, oom_kills=1)


def test_repeated_limits_on_the_same_command_escalate_the_wording():
    policy = FeedbackPolicy()
    messages = [
        policy.evaluate("pytest tests/", Intent(), stall_s=1.0, duration_s=2.0, peak_memory_mb=900.0)
        for _ in range(3)
    ]
    assert "warning number" not in messages[0]
    assert "warning number 3" in messages[2]


def test_an_unobservable_backend_never_speaks():
    policy = FeedbackPolicy()
    assert policy.evaluate(
        "pytest", Intent(), stall_s=9.0, duration_s=1.0, peak_memory_mb=999.0, observable=False
    ) is None


def test_feedback_names_the_next_tier_up():
    intent = resolve_intent("memory:low", env={}, total_bytes=SIXTEEN_GB)
    message = render_feedback(intent, stall_s=1.0, duration_s=2.0, peak_memory_mb=500.0)
    assert "memory:medium" in message


def test_feedback_at_max_tier_does_not_promise_a_higher_one():
    intent = resolve_intent("memory:max", env={}, total_bytes=SIXTEEN_GB)
    message = render_feedback(intent, stall_s=1.0, duration_s=2.0, peak_memory_mb=500.0)
    assert "no memory limit" in message
    assert "memory:max" in message
