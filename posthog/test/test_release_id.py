import contextlib
import os
import unittest
from unittest import mock

import pytest
from parameterized import parameterized

from posthog import AsyncPosthog
from posthog.client import _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES, Client
from posthog.release_id import RELEASE_ID_ENV_VAR, _resolve_release_id
from posthog.test.test_utils import FAKE_TEST_API_KEY

# (name, call, expected event): one row per public event-producing method, shared
# by the sync and async clients. Each call builds its own arguments, because a
# captured exception is marked and a second capture of the same object is skipped.
EVENT_CALLS = [
    (
        "capture",
        lambda client: client.capture("python test event", distinct_id="user-1"),
        "python test event",
    ),
    (
        "capture_exception",
        lambda client: client.capture_exception(Exception("boom")),
        "$exception",
    ),
    (
        "set",
        lambda client: client.set(distinct_id="user-1", properties={"plan": "pro"}),
        "$set",
    ),
    (
        "set_once",
        lambda client: client.set_once(
            distinct_id="user-1", properties={"first_seen": True}
        ),
        "$set_once",
    ),
    (
        "alias",
        lambda client: client.alias(previous_id="anon-1", distinct_id="user-1"),
        "$create_alias",
    ),
    (
        "group_identify",
        lambda client: client.group_identify(
            group_type="company", group_key="company-1"
        ),
        "$groupidentify",
    ),
]


@contextlib.contextmanager
def _release_id_env(value):
    """Set POSTHOG_RELEASE_ID to `value` (unset when None) for the block."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(RELEASE_ID_ENV_VAR, None)
        if value is not None:
            os.environ[RELEASE_ID_ENV_VAR] = value
        yield


class TestResolveReleaseId(unittest.TestCase):
    @parameterized.expand(
        [
            ("unset", None, None),
            ("set", "0198c1a2-release", "0198c1a2-release"),
            ("padded", "  0198c1a2-release\n", "0198c1a2-release"),
            ("empty", "", None),
            ("whitespace", "   ", None),
        ]
    )
    def test_env_var_resolution(self, _name, env_value, expected) -> None:
        with _release_id_env(env_value):
            self.assertEqual(_resolve_release_id(), expected)


class TestClientReleaseId(unittest.TestCase):
    def _client(self, env_value, **kwargs):
        """Build a client under `env_value` and collect the events it would send."""
        events = []

        def before_send(msg):
            events.append(msg)
            return msg

        with _release_id_env(env_value):
            client = Client(
                FAKE_TEST_API_KEY, send=False, before_send=before_send, **kwargs
            )
        return client, events

    @parameterized.expand(EVENT_CALLS)
    def test_release_id_is_attached_to_every_event(
        self, _name, call, expected_event
    ) -> None:
        client, events = self._client("0198c1a2-release")
        call(client)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], expected_event)
        self.assertEqual(events[0]["properties"]["$release_id"], "0198c1a2-release")

    @parameterized.expand([("unset", None), ("blank", "  ")])
    def test_no_release_id_is_sent_without_a_value(self, _name, env_value) -> None:
        client, events = self._client(env_value)
        client.capture("python test event", distinct_id="user-1")
        client.capture_exception(Exception("boom"))

        self.assertEqual(len(events), 2)
        for event in events:
            self.assertNotIn("$release_id", event["properties"])

    def test_explicit_release_id_property_wins_over_the_env_var(self) -> None:
        client, events = self._client("from-env")
        client.capture(
            "python test event",
            distinct_id="user-1",
            properties={"$release_id": "from-caller"},
        )
        self.assertEqual(events[0]["properties"]["$release_id"], "from-caller")

    def test_super_property_release_id_wins_over_the_env_var(self) -> None:
        client, events = self._client(
            "from-env", super_properties={"$release_id": "from-super"}
        )
        client.capture("python test event", distinct_id="user-1")
        self.assertEqual(events[0]["properties"]["$release_id"], "from-super")

    def test_release_id_is_read_once_at_client_init(self) -> None:
        client, events = self._client("at-init")
        with _release_id_env("changed-later"):
            client.capture("python test event", distinct_id="user-1")
        self.assertEqual(events[0]["properties"]["$release_id"], "at-init")

    def test_minimal_flag_called_events_keep_their_strict_allowlist(self) -> None:
        client, events = self._client("0198c1a2-release")
        client._enqueue(
            {
                "event": "$feature_flag_called",
                "distinct_id": "user-1",
                "timestamp": None,
                "properties": {"$feature_flag": "my-flag"},
            },
            None,
            property_allowlist=_MINIMAL_FLAG_CALLED_EVENT_PROPERTIES,
        )
        self.assertEqual(events[0]["properties"]["$feature_flag"], "my-flag")
        self.assertNotIn("$release_id", events[0]["properties"])


async def _async_events(env_value, send_events):
    """Build an async client under `env_value`, run `send_events`, return the batch."""
    batches = []

    async def batch_post(*args, **kwargs):
        batches.append(kwargs["batch"])

    with mock.patch("posthog._async_consumer.async_batch_post", side_effect=batch_post):
        with _release_id_env(env_value):
            client = AsyncPosthog("test-key", flush_interval=30)
        async with client:
            send_events(client)
            await client.flush(timeout_seconds=1)
    return [event for batch in batches for event in batch]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "expected_event"),
    [pytest.param(call, event, id=name) for name, call, event in EVENT_CALLS],
)
async def test_async_client_attaches_release_id_to_every_event(call, expected_event):
    events = await _async_events("0198c1a2-release", call)

    assert len(events) == 1
    assert events[0]["event"] == expected_event
    assert events[0]["properties"]["$release_id"] == "0198c1a2-release"


@pytest.mark.asyncio
async def test_async_client_sends_no_release_id_without_a_value():
    events = await _async_events(
        None, lambda client: client.capture("event", distinct_id="user-1")
    )
    assert "$release_id" not in events[0]["properties"]


@pytest.mark.asyncio
async def test_async_client_explicit_release_id_property_wins_over_the_env_var():
    events = await _async_events(
        "from-env",
        lambda client: client.capture(
            "event", distinct_id="user-1", properties={"$release_id": "from-caller"}
        ),
    )
    assert events[0]["properties"]["$release_id"] == "from-caller"
