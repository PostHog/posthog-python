import os
import unittest
from unittest import mock

from parameterized import parameterized

from posthog.capture_compression import (
    CAPTURE_COMPRESSION_ENV_VAR,
    CaptureCompression,
    resolve_capture_compression,
)
from posthog.client import Client
from posthog.consumer import Consumer
from posthog.test.logging_helpers import capture_message_only_logs
from posthog.test.test_utils import TEST_API_KEY


class TestResolveCaptureCompression(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(CAPTURE_COMPRESSION_ENV_VAR, None)

    def test_defaults_to_none_with_no_kwarg_env_or_gzip(self) -> None:
        self.assertIs(resolve_capture_compression(None), CaptureCompression.NONE)

    def test_gzip_fallback_used_when_nothing_else_set(self) -> None:
        self.assertIs(
            resolve_capture_compression(None, gzip_fallback=True),
            CaptureCompression.GZIP,
        )

    @parameterized.expand(
        [
            ("enum_gzip", CaptureCompression.GZIP, CaptureCompression.GZIP),
            ("enum_deflate", CaptureCompression.DEFLATE, CaptureCompression.DEFLATE),
            ("enum_none", CaptureCompression.NONE, CaptureCompression.NONE),
            ("str_gzip", "gzip", CaptureCompression.GZIP),
            ("str_deflate", "deflate", CaptureCompression.DEFLATE),
            ("str_none", "none", CaptureCompression.NONE),
            ("str_identity_alias", "identity", CaptureCompression.NONE),
            ("str_upper_and_padded", "  GZIP  ", CaptureCompression.GZIP),
        ]
    )
    def test_explicit_kwarg_takes_precedence_and_coerces(
        self, _name, kwarg, expected
    ) -> None:
        # Env names a different value and gzip_fallback is on, so each row proves
        # the explicit kwarg wins over both lower-precedence sources.
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: "deflate"}):
            self.assertIs(
                resolve_capture_compression(kwarg, gzip_fallback=True), expected
            )

    def test_invalid_kwarg_raises_even_with_valid_env(self) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: "gzip"}):
            with self.assertRaises(ValueError):
                resolve_capture_compression("bogus")

    @parameterized.expand([("bad_str", "bogus"), ("wrong_type", 1)])
    def test_invalid_explicit_kwarg_raises(self, _name, value) -> None:
        with self.assertRaises(ValueError):
            resolve_capture_compression(value)

    @parameterized.expand(
        [
            ("gzip", "gzip", CaptureCompression.GZIP),
            ("deflate", "deflate", CaptureCompression.DEFLATE),
            ("none", "none", CaptureCompression.NONE),
            ("identity", "identity", CaptureCompression.NONE),
            ("uppercase", "GZIP", CaptureCompression.GZIP),
            ("padded", "  deflate ", CaptureCompression.DEFLATE),
        ]
    )
    def test_env_var_resolution(self, _name, env_value, expected) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: env_value}):
            self.assertIs(resolve_capture_compression(None), expected)

    def test_env_var_takes_precedence_over_gzip_fallback(self) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: "deflate"}):
            self.assertIs(
                resolve_capture_compression(None, gzip_fallback=True),
                CaptureCompression.DEFLATE,
            )

    @parameterized.expand([("empty", ""), ("whitespace", "   ")])
    def test_blank_env_var_falls_through_to_fallback(self, _name, env_value) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: env_value}):
            self.assertIs(resolve_capture_compression(None), CaptureCompression.NONE)
            self.assertIs(
                resolve_capture_compression(None, gzip_fallback=True),
                CaptureCompression.GZIP,
            )

    def test_unrecognized_env_var_warns_and_uses_fallback(self) -> None:
        with mock.patch.dict(os.environ, {CAPTURE_COMPRESSION_ENV_VAR: "bogus"}):
            with capture_message_only_logs() as stream:
                self.assertIs(
                    resolve_capture_compression(None, gzip_fallback=True),
                    CaptureCompression.GZIP,
                )
        self.assertIn("bogus", stream.getvalue())


class TestCaptureCompressionPlumbing(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(CAPTURE_COMPRESSION_ENV_VAR, None)

    def test_client_defaults_to_none(self) -> None:
        client = Client(TEST_API_KEY, sync_mode=True)
        self.assertIs(client.capture_compression, CaptureCompression.NONE)

    def test_client_gzip_flag_falls_back_to_gzip(self) -> None:
        client = Client(TEST_API_KEY, sync_mode=True, gzip=True)
        self.assertIs(client.capture_compression, CaptureCompression.GZIP)

    @parameterized.expand(
        [
            ("enum_deflate", CaptureCompression.DEFLATE, CaptureCompression.DEFLATE),
            ("str_gzip", "gzip", CaptureCompression.GZIP),
            ("str_none", "none", CaptureCompression.NONE),
        ]
    )
    def test_client_kwarg_overrides_gzip_flag(self, _name, kwarg, expected) -> None:
        # Even with the legacy gzip flag on, the explicit kwarg wins.
        client = Client(
            TEST_API_KEY, sync_mode=True, gzip=True, capture_compression=kwarg
        )
        self.assertIs(client.capture_compression, expected)

    def test_client_propagates_to_consumers(self) -> None:
        client = Client(
            TEST_API_KEY,
            capture_compression=CaptureCompression.DEFLATE,
            send=False,
            thread=2,
        )
        self.assertEqual(len(client.consumers), 2)
        for consumer in client.consumers:
            self.assertIs(consumer.capture_compression, CaptureCompression.DEFLATE)

    def test_consumer_defaults_to_none(self) -> None:
        consumer = Consumer(None, TEST_API_KEY)
        self.assertIs(consumer.capture_compression, CaptureCompression.NONE)
