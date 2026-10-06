import importlib
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

import posthog
from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.utils import clean


@pytest.mark.parametrize("method", ["capture", "capture_ai"])
@pytest.mark.parametrize("should_capture", [None, lambda: True])
def test_allowed_events_are_cleaned_once(method, should_capture):
    client = Client(FAKE_TEST_API_KEY, should_capture=should_capture, sync_mode=True)
    with (
        mock.patch("posthog.client.clean", wraps=clean) as clean_payload,
        mock.patch("posthog.client.batch_post") as post,
    ):
        event_uuid = getattr(client, method)(
            "example_event", distinct_id="user1", properties={"nested": [{"value": 1}]}
        )

    assert event_uuid is not None
    clean_payload.assert_called_once()
    post.assert_called_once()
    assert post.call_args.kwargs["batch"][0]["properties"]["nested"] == [{"value": 1}]


@pytest.mark.parametrize("method", ["capture", "capture_ai"])
@pytest.mark.parametrize("result", [False, None, 1, "true"])
def test_rejected_events_skip_normalization_cleaning_and_before_send(method, result):
    before_send = mock.Mock()
    client = Client(
        FAKE_TEST_API_KEY,
        should_capture=lambda: result,
        before_send=before_send,
        sync_mode=True,
    )
    with (
        mock.patch("posthog.client.clean") as clean_payload,
        mock.patch.object(client, "_normalize_event_uuid") as normalize_uuid,
        mock.patch("posthog.client.batch_post") as post,
    ):
        assert getattr(client, method)("example_event", distinct_id="user1") is None

    clean_payload.assert_not_called()
    normalize_uuid.assert_not_called()
    before_send.assert_not_called()
    post.assert_not_called()


def test_callback_failure_drops_event_without_logging_its_contents(caplog):
    should_capture = mock.Mock(side_effect=ValueError("private exception content"))
    client = Client(FAKE_TEST_API_KEY, should_capture=should_capture, sync_mode=True)
    with mock.patch("posthog.client.batch_post") as post:
        assert client.capture("private event", distinct_id="private user") is None

    post.assert_not_called()
    assert "Error in should_capture callback; dropping event" in caplog.text
    assert "private" not in caplog.text


def test_allowed_events_still_run_and_reclean_mutating_before_send():
    unsupported_value = object()

    def before_send(event):
        event["properties"]["unsupported"] = unsupported_value
        event["properties"]["scrubbed"] = True
        return event

    client = Client(
        FAKE_TEST_API_KEY,
        should_capture=lambda: True,
        before_send=before_send,
        sync_mode=True,
    )
    with (
        mock.patch("posthog.client.clean", wraps=clean) as clean_payload,
        mock.patch("posthog.client.batch_post") as post,
    ):
        assert client.capture("example_event", distinct_id="user1") is not None

    assert clean_payload.call_count == 2
    properties = post.call_args.kwargs["batch"][0]["properties"]
    assert properties["unsupported"] is None
    assert properties["scrubbed"] is True


def test_filter_uses_each_callers_context_without_disabling_the_shared_client():
    private_context = ContextVar("private_context", default=False)
    client = Client(
        FAKE_TEST_API_KEY, should_capture=lambda: not private_context.get(), send=False
    )

    def capture_in_context(private):
        token = private_context.set(private)
        try:
            return client.capture("example_event", distinct_id="user1")
        finally:
            private_context.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(capture_in_context, [True, False, True, False]))

    assert results[0] is None
    assert results[1] is not None
    assert results[2] is None
    assert results[3] is not None
    assert client.disabled is False


def test_filter_survives_client_reinitialization_after_fork():
    should_capture = mock.Mock(return_value=False)
    client = Client(FAKE_TEST_API_KEY, should_capture=should_capture, send=False)

    client._reinit_after_fork()

    assert client.capture("example_event", distinct_id="user1") is None
    should_capture.assert_called_once_with()


def test_module_setting_applies_at_setup_and_after_initialization():
    importlib.reload(posthog)
    try:
        posthog.api_key = FAKE_TEST_API_KEY
        posthog.sync_mode = True
        posthog.should_capture = lambda: False

        with mock.patch("posthog.client.batch_post") as post:
            assert posthog.capture("rejected", distinct_id="user1") is None
            posthog.should_capture = lambda: True
            assert posthog.capture_ai("allowed", distinct_id="user1") is not None
            posthog.should_capture = None
            assert posthog.capture("default", distinct_id="user1") is not None

        assert [call.kwargs["batch"][0]["event"] for call in post.call_args_list] == [
            "allowed",
            "default",
        ]
    finally:
        if posthog.default_client:
            posthog.shutdown()
        importlib.reload(posthog)
