import importlib
import warnings
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


@pytest.mark.parametrize("allowed", [False, True])
def test_feature_flags_are_requested_only_for_allowed_events(allowed):
    should_capture = mock.Mock(return_value=allowed)
    client = Client(FAKE_TEST_API_KEY, should_capture=should_capture, send=False)
    with (
        mock.patch("posthog.client.flags", return_value={"featureFlags": {}}) as flags,
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        event_uuid = client.capture(
            "example_event", distinct_id="user1", send_feature_flags=True
        )

    assert (event_uuid is not None) is allowed
    assert flags.call_count == int(allowed)
    should_capture.assert_called_once_with()


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


@pytest.mark.parametrize(
    "access_flag",
    [
        lambda client: client.get_feature_flag_result(
            "beta-feature", "user1", only_evaluate_locally=True
        ),
        lambda client: client.evaluate_flags(
            "user1", only_evaluate_locally=True
        ).is_enabled("beta-feature"),
    ],
    ids=["get_feature_flag_result", "evaluate_flags"],
)
def test_rejected_flag_exposure_is_still_sent_from_an_allowed_context(access_flag):
    private_context = ContextVar("private_context", default=False)
    sent_events = []

    def before_send(event):
        sent_events.append(event["event"])
        return event

    client = Client(
        FAKE_TEST_API_KEY,
        secret_key="test",
        should_capture=lambda: not private_context.get(),
        before_send=before_send,
        send=False,
    )
    client.feature_flags = [
        {
            "id": 1,
            "key": "beta-feature",
            "active": True,
            "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
        }
    ]

    token = private_context.set(True)
    try:
        access_flag(client)
    finally:
        private_context.reset(token)
    assert sent_events == []

    access_flag(client)
    access_flag(client)
    assert sent_events == ["$feature_flag_called"]


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
