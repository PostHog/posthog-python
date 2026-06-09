import pytest
from posthog.rate_limiter import ExceptionRateLimiter


class FakeClock:
    """A clean, predictable clock mock for simulating time progression without sleeps."""

    def __init__(self, start: float = 0.0):
        self.now = float(start)

    def advance(self, seconds: float):
        self.now += float(seconds)

    def __call__(self) -> float:
        return self.now


def test_allows_within_limit_and_handles_heartbeat():
    """Verify that the first N events pass wide open, and subsequent events

    are aggressively throttled to a rhythmic heartbeat ratio.
    """
    clock = FakeClock(0.0)
    # Allow 5 events per window, then allow every 10th event thereafter
    rl = ExceptionRateLimiter(
        max_exceptions=5, window_seconds=60.0, post_limit_every=10, clock=clock
    )

    # 1. First 5 events must be allowed through cleanly
    for i in range(5):
        assert rl.should_capture() is True, (
            f"Event {i + 1} should be captured within max limits"
        )

    # 2. Events 6 through 9 must be completely blocked by the emergency brake
    for i in range(4):
        assert rl.should_capture() is False, (
            f"Event {i + 6} should be blocked after max limits"
        )

    # 3. The 10th total event triggers the heartbeat check (10 % 10 == 0) and passes
    assert rl.should_capture() is True, (
        "The 10th event should act as a heartbeat signal"
    )

    # 4. The next 9 events (11 through 19) are dropped
    for i in range(9):
        assert rl.should_capture() is False, (
            f"Event {i + 11} should be blocked during heartbeat cooldown"
        )

    # 5. The 20th total event triggers the next heartbeat check (20 % 10 == 0) and passes
    assert rl.should_capture() is True, (
        "The 20th event should act as a heartbeat signal"
    )


def test_window_resets_counters_cleanly():
    """Verify that once the time window boundary is crossed, the counter

    completely clears and opens the gate wide again.
    """
    clock = FakeClock(0.0)
    rl = ExceptionRateLimiter(
        max_exceptions=2, window_seconds=10.0, post_limit_every=10, clock=clock
    )

    # Fill up the current window capacity
    assert rl.should_capture() is True  # Count = 1 (Allowed)
    assert rl.should_capture() is True  # Count = 2 (Allowed)
    assert rl.should_capture() is False  # Count = 3 (Blocked hard!)

    # Advance time past the 10.0-second configuration limit
    clock.advance(10.1)

    # The rate limiter must reset internal tracking counters to 0
    assert rl.should_capture() is True, "First event in fresh window should pass"
    assert rl.should_capture() is True, "Second event in fresh window should pass"
    assert rl.should_capture() is False, "Third event in fresh window should block"


def test_invalid_parameters_raise_value_errors():
    """Verify that the class initialization blocks dirty, negative, or zero configuration parameters."""
    with pytest.raises(ValueError, match="max_exceptions must be >= 0"):
        ExceptionRateLimiter(max_exceptions=-1)

    with pytest.raises(ValueError, match="window_seconds must be > 0"):
        ExceptionRateLimiter(window_seconds=0)

    with pytest.raises(ValueError, match="window_seconds must be > 0"):
        ExceptionRateLimiter(window_seconds=-5.5)

    with pytest.raises(ValueError, match="post_limit_every must be > 0"):
        ExceptionRateLimiter(post_limit_every=0)


def test_post_every_one_allows_all_events_after_limit():
    """Verify that setting post_limit_every to 1 acts as an analytical bypass,

    allowing everything through after the threshold cap is blown.
    """
    clock = FakeClock(0.0)
    rl = ExceptionRateLimiter(
        max_exceptions=0, window_seconds=10.0, post_limit_every=1, clock=clock
    )

    # Because max=0, all events are post-limit, but since post_limit_every=1, everything passes (N % 1 == 0)
    assert rl.should_capture() is True
    assert rl.should_capture() is True
    assert rl.should_capture() is True
