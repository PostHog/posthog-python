import threading
from unittest.mock import MagicMock

import pytest

from posthog.bucketed_rate_limiter import BucketedRateLimiter


class FakeClock:
    def __init__(self, start=0.0):
        self.now = float(start)

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def make_limiter(
    clock, bucket_size=10, refill_rate=1, refill_interval_seconds=1, **kwargs
):
    return BucketedRateLimiter(
        bucket_size=bucket_size,
        refill_rate=refill_rate,
        refill_interval_seconds=refill_interval_seconds,
        clock=clock,
        **kwargs,
    )


def test_not_rate_limited_by_default():
    assert make_limiter(FakeClock()).consume_rate_limit("ResizeObserver") is False


@pytest.mark.parametrize("bucket_size", [1, 5, 10, 50])
def test_exhausts_bucket_after_bucket_size_consumptions(bucket_size):
    limiter = make_limiter(FakeClock(), bucket_size=bucket_size)

    # the call that drains the bucket is itself rate limited, so
    # bucket_size - 1 events pass
    for _ in range(bucket_size - 1):
        assert limiter.consume_rate_limit("test") is False

    assert limiter.consume_rate_limit("test") is True
    # can check the same bucket more than once
    assert limiter.consume_rate_limit("test") is True


def test_refills_tokens_based_on_elapsed_time():
    clock = FakeClock()
    limiter = make_limiter(clock)

    for _ in range(9):
        assert limiter.consume_rate_limit("key") is False
    assert limiter.consume_rate_limit("key") is True

    clock.advance(2)

    assert limiter.consume_rate_limit("key") is False
    assert limiter._buckets["key"].tokens == 1


def test_refills_to_bucket_size_maximum():
    clock = FakeClock()
    limiter = make_limiter(clock)
    limiter.consume_rate_limit("key")

    clock.advance(20)

    limiter.consume_rate_limit("key")
    assert limiter._buckets["key"].tokens == 9


def test_partial_refill_intervals_do_not_refill_tokens():
    clock = FakeClock()
    limiter = make_limiter(clock)

    for _ in range(9):
        limiter.consume_rate_limit("test")

    clock.advance(0.999)

    limiter.consume_rate_limit("test")
    assert limiter._buckets["test"].tokens == 0


@pytest.mark.parametrize(
    "refill_rate, intervals, tokens_left, expected",
    [
        (1, 1, 9, 9),
        (2, 1, 9, 9),
        (1, 2, 9, 9),
        (3, 1, 5, 7),
        (2, 2, 5, 8),
    ],
)
def test_refill_rates(refill_rate, intervals, tokens_left, expected):
    clock = FakeClock()
    limiter = make_limiter(clock, refill_rate=refill_rate)

    for _ in range(10 - tokens_left):
        limiter.consume_rate_limit("test")

    clock.advance(intervals)

    limiter.consume_rate_limit("test")
    assert limiter._buckets["test"].tokens == expected


def test_different_keys_maintain_separate_buckets():
    limiter = make_limiter(FakeClock())

    for _ in range(9):
        limiter.consume_rate_limit("bucket1")

    assert limiter.consume_rate_limit("bucket1") is True
    assert limiter.consume_rate_limit("bucket2") is False

    assert limiter._buckets["bucket1"].tokens == 0
    assert limiter._buckets["bucket2"].tokens == 9


def test_invokes_callback_once_when_bucket_reaches_zero():
    callback = MagicMock()
    limiter = make_limiter(FakeClock(), bucket_size=3, on_bucket_rate_limited=callback)

    limiter.consume_rate_limit("test")
    limiter.consume_rate_limit("test")
    callback.assert_not_called()

    limiter.consume_rate_limit("test")
    callback.assert_called_once_with("test")

    # not invoked again while the bucket stays empty
    limiter.consume_rate_limit("test")
    callback.assert_called_once()


def test_invokes_callback_again_after_refill_and_re_exhaustion():
    callback = MagicMock()
    clock = FakeClock()
    limiter = make_limiter(clock, bucket_size=2, on_bucket_rate_limited=callback)

    limiter.consume_rate_limit("test")
    limiter.consume_rate_limit("test")
    assert callback.call_count == 1

    clock.advance(2)

    limiter.consume_rate_limit("test")
    limiter.consume_rate_limit("test")
    assert callback.call_count == 2


def test_stop_clears_all_buckets_and_resets_state():
    clock = FakeClock()
    limiter = make_limiter(clock)

    for _ in range(9):
        limiter.consume_rate_limit("test")
    limiter.consume_rate_limit("other")
    assert len(limiter._buckets) == 2

    limiter.stop()
    assert len(limiter._buckets) == 0

    assert limiter.consume_rate_limit("test") is False
    assert limiter._buckets["test"].tokens == 9


def test_last_access_advances_by_complete_intervals_preserving_fraction():
    clock = FakeClock()
    limiter = make_limiter(clock)

    limiter.consume_rate_limit("test")
    assert limiter._buckets["test"].last_access == 0.0
    assert limiter._buckets["test"].tokens == 9

    clock.advance(0.5)
    limiter.consume_rate_limit("test")
    assert limiter._buckets["test"].last_access == 0.0
    assert limiter._buckets["test"].tokens == 8

    clock.advance(0.6)
    limiter.consume_rate_limit("test")
    assert limiter._buckets["test"].last_access == 1.0
    assert limiter._buckets["test"].tokens == 8


def test_clamps_out_of_range_options():
    clock = FakeClock()
    limiter = make_limiter(
        clock, bucket_size=1000, refill_rate=-5, refill_interval_seconds="nope"
    )

    assert limiter._bucket_size == 100
    assert limiter._refill_rate == 0
    assert limiter._refill_interval == 86400.0


def test_thread_safety_allows_exactly_bucket_size_minus_one():
    limiter = make_limiter(FakeClock(), bucket_size=50)
    allowed = []

    def consume():
        for _ in range(20):
            if limiter.consume_rate_limit("shared") is False:
                allowed.append(1)

    threads = [threading.Thread(target=consume) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(allowed) == 49


def test_exception_capture_default_configuration():
    from posthog.exception_capture import ExceptionCapture

    capture = ExceptionCapture(MagicMock())
    try:
        assert capture._rate_limiter._bucket_size == 50
        assert capture._rate_limiter._refill_rate == 10
        assert capture._rate_limiter._refill_interval == 10
    finally:
        capture.close()


def test_exception_capture_rate_limiting_is_configurable():
    from posthog.exception_capture import ExceptionCapture

    capture = ExceptionCapture(
        MagicMock(), bucket_size=3, refill_rate=2, refill_interval_seconds=5
    )
    try:
        assert capture._rate_limiter._bucket_size == 3
        assert capture._rate_limiter._refill_rate == 2
        assert capture._rate_limiter._refill_interval == 5
    finally:
        capture.close()


def test_client_passes_rate_limiter_configuration_through():
    from posthog.client import Client

    client = Client(
        "phc_test",
        sync_mode=True,
        disabled=True,
        enable_exception_autocapture=True,
        exception_autocapture_bucket_size=3,
        exception_autocapture_refill_rate=2,
        exception_autocapture_refill_interval_seconds=5,
    )
    try:
        limiter = client.exception_capture._rate_limiter
        assert limiter._bucket_size == 3
        assert limiter._refill_rate == 2
        assert limiter._refill_interval == 5
    finally:
        client.shutdown()


def test_exception_capture_rate_limits_per_exception_type():
    from posthog.exception_capture import ExceptionCapture

    client = MagicMock()
    capture = ExceptionCapture(client, bucket_size=10)
    try:

        def exc_info(error):
            try:
                raise error
            except type(error):
                import sys

                return sys.exc_info()

        for _ in range(15):
            capture.capture_exception(exc_info(ValueError("boom")))

        # bucket size 10 -> 9 captured, the rest rate limited
        assert client.capture_exception.call_count == 9

        # a different exception type has its own bucket
        capture.capture_exception(exc_info(ZeroDivisionError("zero")))
        assert client.capture_exception.call_count == 10
    finally:
        capture.close()
