import os
import unittest
from unittest import mock

from parameterized import parameterized

from posthog.capture_mode import (
    CAPTURE_MODE_ENV_VAR,
    CaptureMode,
    resolve_capture_mode,
)
from posthog.client import Client
from posthog.consumer import Consumer
from posthog.test.logging_helpers import capture_message_only_logs
from posthog.test.test_utils import TEST_API_KEY


class TestResolveCaptureMode(unittest.TestCase):
    def test_defaults_to_v0_with_no_kwarg_and_no_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CAPTURE_MODE_ENV_VAR, None)
            self.assertIs(resolve_capture_mode(None), CaptureMode.V0)

    @parameterized.expand(
        [
            ("enum_v0", CaptureMode.V0, CaptureMode.V0),
            ("enum_v1", CaptureMode.V1, CaptureMode.V1),
            ("str_v0", "v0", CaptureMode.V0),
            ("str_v1", "v1", CaptureMode.V1),
            ("str_legacy_alias", "legacy", CaptureMode.V0),
            ("str_analytics_v1_alias", "analytics_v1", CaptureMode.V1),
            ("str_upper_and_padded", "  V1  ", CaptureMode.V1),
        ]
    )
    def test_explicit_kwarg_takes_precedence_and_coerces(
        self, _name, kwarg, expected
    ) -> None:
        # Env is set to the opposite mode to prove the kwarg wins.
        with mock.patch.dict(os.environ, {CAPTURE_MODE_ENV_VAR: "v1"}):
            self.assertIs(resolve_capture_mode(kwarg), expected)

    @parameterized.expand(
        [
            ("v0", "v0", CaptureMode.V0),
            ("legacy", "legacy", CaptureMode.V0),
            ("v1", "v1", CaptureMode.V1),
            ("analytics_v1", "analytics_v1", CaptureMode.V1),
            ("uppercase", "V1", CaptureMode.V1),
            ("padded", "  v1 ", CaptureMode.V1),
        ]
    )
    def test_env_var_resolution(self, _name, env_value, expected) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_MODE_ENV_VAR: env_value}):
            self.assertIs(resolve_capture_mode(None), expected)

    @parameterized.expand([("empty", ""), ("whitespace", "   ")])
    def test_blank_env_var_defaults_to_v0(self, _name, env_value) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_MODE_ENV_VAR: env_value}):
            self.assertIs(resolve_capture_mode(None), CaptureMode.V0)

    def test_unrecognized_env_var_warns_and_defaults_to_v0(self) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_MODE_ENV_VAR: "bogus"}):
            with capture_message_only_logs() as stream:
                self.assertIs(resolve_capture_mode(None), CaptureMode.V0)
        self.assertIn("bogus", stream.getvalue())

    @parameterized.expand([("bad_str", "bogus"), ("wrong_type", 1)])
    def test_invalid_explicit_kwarg_raises(self, _name, value) -> None:
        with self.assertRaises(ValueError):
            resolve_capture_mode(value)


class TestCaptureModePlumbing(unittest.TestCase):
    def test_client_resolves_and_stores_default_v0(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CAPTURE_MODE_ENV_VAR, None)
            client = Client(TEST_API_KEY, sync_mode=True)
        self.assertIs(client.capture_mode, CaptureMode.V0)

    @parameterized.expand(
        [
            ("enum_v1", CaptureMode.V1, CaptureMode.V1),
            ("str_v1", "v1", CaptureMode.V1),
            ("enum_v0", CaptureMode.V0, CaptureMode.V0),
        ]
    )
    def test_client_kwarg_sets_mode(self, _name, kwarg, expected) -> None:
        client = Client(TEST_API_KEY, sync_mode=True, capture_mode=kwarg)
        self.assertIs(client.capture_mode, expected)

    def test_client_propagates_mode_to_consumers(self) -> None:
        # Async (non-sync) client builds Consumer threads; assert each carries
        # the resolved mode.
        client = Client(TEST_API_KEY, capture_mode=CaptureMode.V1, send=False, thread=2)
        self.assertEqual(len(client.consumers), 2)
        for consumer in client.consumers:
            self.assertIs(consumer.capture_mode, CaptureMode.V1)

    def test_consumer_defaults_to_v0(self) -> None:
        consumer = Consumer(None, TEST_API_KEY)
        self.assertIs(consumer.capture_mode, CaptureMode.V0)
