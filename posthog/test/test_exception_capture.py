import subprocess
import sys
from textwrap import dedent
from unittest.mock import MagicMock

import pytest


def _exc_info(error):
    try:
        raise error
    except BaseException:
        return sys.exc_info()


def _chained_exc_info(cause, wrapper):
    try:
        raise wrapper from cause
    except BaseException:
        return sys.exc_info()


def test_rate_limiting_is_disabled_by_default():
    from posthog.exception_capture import ExceptionCapture

    client = MagicMock()
    capture = ExceptionCapture(client)
    try:
        assert capture._rate_limiter is None

        for _ in range(100):
            capture.capture_exception(_exc_info(ValueError("boom")))
        assert client.capture_exception.call_count == 100
    finally:
        capture.close()


def test_rate_limiting_default_configuration_when_enabled():
    from posthog.exception_capture import ExceptionCapture

    capture = ExceptionCapture(MagicMock(), rate_limiting_enabled=True)
    try:
        assert capture._rate_limiter._bucket_size == 50
        assert capture._rate_limiter._refill_rate == 10
        assert capture._rate_limiter._refill_interval == 10
    finally:
        capture.close()


def test_rate_limiting_is_configurable():
    from posthog.exception_capture import ExceptionCapture

    capture = ExceptionCapture(
        MagicMock(),
        rate_limiting_enabled=True,
        bucket_size=3,
        refill_rate=2,
        refill_interval_seconds=5,
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
        enable_exception_autocapture_rate_limiting=True,
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


def test_rate_limits_per_exception_type():
    from posthog.exception_capture import ExceptionCapture

    client = MagicMock()
    capture = ExceptionCapture(client, rate_limiting_enabled=True, bucket_size=10)
    try:
        for _ in range(15):
            capture.capture_exception(_exc_info(ValueError("boom")))

        # bucket size 10 -> 9 captured, the rest rate limited
        assert client.capture_exception.call_count == 9

        # a different exception type has its own bucket
        capture.capture_exception(_exc_info(ZeroDivisionError("zero")))
        assert client.capture_exception.call_count == 10
    finally:
        capture.close()


def test_rate_limit_keys_on_root_cause_of_chained_exceptions():
    from posthog.exception_capture import ExceptionCapture

    # PostHog groups by the root cause ($exception_list[0].type), so chained
    # exceptions sharing a wrapper type but differing in their cause must land
    # in separate buckets rather than collapsing under the wrapper.
    client = MagicMock()
    capture = ExceptionCapture(client, rate_limiting_enabled=True, bucket_size=2)
    try:
        capture.capture_exception(
            _chained_exc_info(ZeroDivisionError(), RuntimeError("wrapped"))
        )
        capture.capture_exception(
            _chained_exc_info(KeyError(), RuntimeError("wrapped"))
        )

        assert client.capture_exception.call_count == 2
    finally:
        capture.close()


def test_excepthook(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    from posthog import Posthog
    posthog = Posthog('phc_x', host='https://eu.i.posthog.com', enable_exception_autocapture=True, debug=True, on_error=lambda e, batch: print('error handling batch: ', e, batch))

    # frame_value = "LOL"

    1/0
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output

    assert b"ZeroDivisionError" in output
    assert b"LOL" in output
    assert b"DEBUG:posthog:[PostHog] data uploaded successfully" in output
    assert (
        b'"$exception_list": [{"mechanism": {"type": "generic", "handled": true}, "module": null, "type": "ZeroDivisionError", "value": "division by zero", "stacktrace": {"frames": [{"platform": "python", "filename": "app.py", "abs_path"'
        in output
    )


class _RootError(Exception):
    pass


class _WrapperError(Exception):
    pass


class _LeafOne(Exception):
    pass


class _LeafTwo(Exception):
    pass


def test_exception_list_canonical_order_explicit_cause():
    # Canonical ordering: $exception_list[0] is the caught/outermost exception
    # and the root cause is last. For `raise B from A`, B is caught and A is the
    # root cause.
    from posthog.exception_utils import exceptions_from_error_tuple

    try:
        try:
            raise _RootError("root")
        except _RootError as root:
            raise _WrapperError("wrapper") from root
    except _WrapperError:
        exc_info = sys.exc_info()

    exceptions = exceptions_from_error_tuple(exc_info)

    types = [e["type"] for e in exceptions]
    assert types == ["_WrapperError", "_RootError"]
    assert exceptions[0]["value"] == "wrapper"
    assert exceptions[-1]["value"] == "root"


def test_exception_list_canonical_order_implicit_context():
    # Implicit chaining (an exception raised while handling another) uses
    # `__context__`. The caught exception is still first, root cause last.
    from posthog.exception_utils import exceptions_from_error_tuple

    try:
        try:
            raise _RootError("root")
        except _RootError:
            raise _WrapperError("wrapper")
    except _WrapperError:
        exc_info = sys.exc_info()

    exceptions = exceptions_from_error_tuple(exc_info)

    types = [e["type"] for e in exceptions]
    assert types == ["_WrapperError", "_RootError"]
    assert exceptions[0]["value"] == "wrapper"
    assert exceptions[-1]["value"] == "root"


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="ExceptionGroup requires Python 3.11+",
)
def test_exception_list_canonical_order_exception_group():
    # For an ExceptionGroup the group is the caught/outermost exception and
    # comes first, with its member exceptions following.
    from posthog.exception_utils import exceptions_from_error_tuple

    try:
        raise ExceptionGroup(  # noqa: F821 -- builtin on 3.11+
            "group", [_LeafOne("one"), _LeafTwo("two")]
        )
    except BaseException:
        exc_info = sys.exc_info()

    exceptions = exceptions_from_error_tuple(exc_info)

    types = [e["type"] for e in exceptions]
    assert types[0] == "ExceptionGroup"
    assert types[1:] == ["_LeafOne", "_LeafTwo"]
