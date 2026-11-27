import asyncio
import os
import unittest
from unittest.mock import patch

from posthog.ai_trace_sample_rate import (
    AI_TRACE_SAMPLE_RATE_ENV_VAR,
    _fmix32,
    _fnv1a_32,
    _should_sample_ai_trace,
)
from posthog.async_client import AsyncClient
from posthog.client import Client
from posthog.test.capture_helpers import record_sync_capture_sends


class TestAiTraceSampleRate(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get(AI_TRACE_SAMPLE_RATE_ENV_VAR)
        os.environ.pop(AI_TRACE_SAMPLE_RATE_ENV_VAR, None)

    def tearDown(self):
        if self._orig_env is not None:
            os.environ[AI_TRACE_SAMPLE_RATE_ENV_VAR] = self._orig_env
        else:
            os.environ.pop(AI_TRACE_SAMPLE_RATE_ENV_VAR, None)
        super().tearDown()

    def _create_client(self, **kwargs) -> Client:
        defaults = {"sync_mode": True}
        defaults.update(kwargs)
        client = Client("test_key", **defaults)
        self.addCleanup(client.shutdown)
        return client

    def test_fnv1a_32_and_fmix32_matches_specification(self):
        # Empty string yields FNV offset basis
        self.assertEqual(_fnv1a_32(""), 0x811C9DC5)
        # Deterministic 32-bit unsigned integers
        h1 = _fnv1a_32("trace-uuid-12345")
        h2 = _fnv1a_32("trace-uuid-12345")
        self.assertEqual(h1, h2)
        self.assertGreaterEqual(h1, 0)
        self.assertLess(h1, 4294967296)

        # Murmur3 fmix32 finalizer bounds
        mixed = _fmix32(h1)
        self.assertGreaterEqual(mixed, 0)
        self.assertLess(mixed, 4294967296)

    def test_sequential_trace_ids_distribution_with_fmix32(self):
        # Without fmix32, pure 32-bit FNV-1a clusters sequential IDs (0/100 kept at rate 0.1).
        # With fmix32 finalizer, sequential IDs achieve proper bit avalanche.
        kept_100 = sum(
            1 for i in range(100) if _should_sample_ai_trace(f"trace-{i}", 0.1)
        )
        self.assertEqual(kept_100, 12)  # 12% on 100 items (not 0%)

        kept_1000 = sum(
            1 for i in range(1000) if _should_sample_ai_trace(f"trace-{i}", 0.1)
        )
        self.assertEqual(kept_1000, 108)  # 10.8% on 1000 items (close to target 10%)

    def test_should_sample_ai_trace_extremes(self):
        # Rate 0.0 always drops
        self.assertFalse(_should_sample_ai_trace("any-trace-id", 0.0))
        # Rate 1.0 always keeps
        self.assertTrue(_should_sample_ai_trace("any-trace-id", 1.0))

    def test_sample_rate_precedence_kwarg_over_env(self):
        with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: "0.2"}):
            # Kwarg wins over env var
            client = self._create_client(ai_trace_sample_rate=0.5)
            self.assertEqual(client.ai_trace_sample_rate, 0.5)

            # Env var used when kwarg is None
            client_env = self._create_client()
            self.assertEqual(client_env.ai_trace_sample_rate, 0.2)

        # Default is None when neither is provided
        client_default = self._create_client()
        self.assertIsNone(client_default.ai_trace_sample_rate)

    def test_out_of_range_and_invalid_values_ignored(self):
        # Negative rate ignored with warning
        with self.assertLogs("posthog", level="WARNING") as cm:
            client = self._create_client(ai_trace_sample_rate=-0.1)
            self.assertIsNone(client.ai_trace_sample_rate)
            self.assertTrue(any("out of range" in msg for msg in cm.output))

        # Greater than 1.0 ignored with warning
        with self.assertLogs("posthog", level="WARNING") as cm:
            client = self._create_client(ai_trace_sample_rate=1.5)
            self.assertIsNone(client.ai_trace_sample_rate)
            self.assertTrue(any("out of range" in msg for msg in cm.output))

        # Boolean values (subclass of int) rejected with warning
        with self.assertLogs("posthog", level="WARNING") as cm:
            client = self._create_client(ai_trace_sample_rate=True)
            self.assertIsNone(client.ai_trace_sample_rate)
            self.assertTrue(
                any("Invalid ai_trace_sample_rate" in msg for msg in cm.output)
            )

        # Invalid env var string ignored with warning
        with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: "not_a_float"}):
            with self.assertLogs("posthog", level="WARNING") as cm:
                client = self._create_client()
                self.assertIsNone(client.ai_trace_sample_rate)
                self.assertTrue(
                    any(
                        "Invalid POSTHOG_AI_TRACE_SAMPLE_RATE" in msg
                        for msg in cm.output
                    )
                )

        # Blank env var ignored silently without warning
        with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: "   "}):
            client = self._create_client()
            self.assertIsNone(client.ai_trace_sample_rate)

    def test_nan_and_inf_values_rejected_with_warning(self):
        # float("nan") kwarg must be rejected with warning
        with self.assertLogs("posthog", level="WARNING") as cm:
            client = self._create_client(ai_trace_sample_rate=float("nan"))
            self.assertIsNone(client.ai_trace_sample_rate)
            self.assertTrue(any("out of range" in msg for msg in cm.output))

        # "nan" in env var must be rejected with warning
        with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: "nan"}):
            with self.assertLogs("posthog", level="WARNING") as cm:
                client = self._create_client()
                self.assertIsNone(client.ai_trace_sample_rate)
                self.assertTrue(any("out of range" in msg for msg in cm.output))

        # "NaN" with whitespace in env var
        with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: "  NaN  "}):
            with self.assertLogs("posthog", level="WARNING") as cm:
                client = self._create_client()
                self.assertIsNone(client.ai_trace_sample_rate)
                self.assertTrue(any("out of range" in msg for msg in cm.output))

        # "inf" and "-inf" rejected with warning
        for inf_val in ("inf", "-inf"):
            with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: inf_val}):
                with self.assertLogs("posthog", level="WARNING") as cm:
                    client = self._create_client()
                    self.assertIsNone(client.ai_trace_sample_rate)
                    self.assertTrue(any("out of range" in msg for msg in cm.output))

    def test_overflow_integer_does_not_crash(self):
        # Huge integer that overflows float conversion does not crash Client.__init__
        with self.assertLogs("posthog", level="WARNING") as cm:
            client = self._create_client(ai_trace_sample_rate=10**1000)
            self.assertIsNone(client.ai_trace_sample_rate)
            self.assertTrue(
                any("Invalid ai_trace_sample_rate" in msg for msg in cm.output)
            )

    def test_non_ai_events_never_sampled(self):
        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=0.0)

        # Non-$ai_* events must not be dropped even at sample rate 0.0
        ret1 = client.capture(
            "$pageview",
            distinct_id="user-1",
            properties={
                "$current_url": "https://posthog.com",
                "$ai_trace_id": "trace-1",
            },
        )
        ret2 = client.capture(
            "custom_event",
            distinct_id="user-1",
            properties={"some_key": "val", "$ai_trace_id": "trace-1"},
        )

        self.assertIsNotNone(ret1)
        self.assertIsNotNone(ret2)
        self.assertEqual(len(captured), 2)
        self.assertNotIn("$ai_trace_sample_rate", captured[0].get("properties", {}))
        self.assertNotIn("$ai_trace_sample_rate", captured[1].get("properties", {}))

    def test_ai_events_without_trace_id_are_kept(self):
        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=0.0)

        # $ai_* event without $ai_trace_id is kept
        ret = client.capture(
            "$ai_generation", distinct_id="user-1", properties={"model": "gpt-4o"}
        )

        self.assertIsNotNone(ret)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["event"], "$ai_generation")
        self.assertNotIn("$ai_trace_sample_rate", captured[0].get("properties", {}))

    def test_blank_or_whitespace_trace_id_is_kept(self):
        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=0.0)

        # Blank string trace id is treated as missing and kept
        ret = client.capture(
            "$ai_generation", distinct_id="user-1", properties={"$ai_trace_id": "   "}
        )

        self.assertIsNotNone(ret)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("$ai_trace_sample_rate", captured[0].get("properties", {}))

    def test_falsy_zero_trace_id_is_sampled(self):
        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=0.0)

        # Numeric 0 trace id should be sampled (dropped at rate 0.0), not treated as None
        ret = client.capture(
            "$ai_generation", distinct_id="user-1", properties={"$ai_trace_id": 0}
        )

        self.assertIsNone(ret)
        self.assertEqual(len(captured), 0)

    def test_capture_ai_return_values_and_sample_rate_tag(self):
        captured = record_sync_capture_sends(self)
        # Find trace ids for keep and drop at 0.5
        kept_trace_id = next(
            f"trace-{i}"
            for i in range(100)
            if _should_sample_ai_trace(f"trace-{i}", 0.5)
        )
        dropped_trace_id = next(
            f"trace-{i}"
            for i in range(100)
            if not _should_sample_ai_trace(f"trace-{i}", 0.5)
        )

        client = self._create_client(ai_trace_sample_rate=0.5)

        # Kept trace returns UUID string and attaches sample rate
        uuid_kept = client.capture_ai(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": kept_trace_id},
        )
        self.assertIsInstance(uuid_kept, str)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["properties"]["$ai_trace_sample_rate"], 0.5)

        # Dropped trace returns None
        captured.clear()
        uuid_dropped = client.capture_ai(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": dropped_trace_id},
        )
        self.assertIsNone(uuid_dropped)
        self.assertEqual(len(captured), 0)

    def test_ai_trace_sampling_drops_and_keeps_whole_traces(self):
        kept_trace_id = next(
            f"trace-cohort-{i}"
            for i in range(100)
            if _should_sample_ai_trace(f"trace-cohort-{i}", 0.5)
        )
        dropped_trace_id = next(
            f"trace-cohort-{i}"
            for i in range(100)
            if not _should_sample_ai_trace(f"trace-cohort-{i}", 0.5)
        )

        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=0.5)

        # Emit multiple events for the kept trace — all must be kept with rate property
        client.capture(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": kept_trace_id, "step": 1},
        )
        client.capture(
            "$ai_span",
            distinct_id="user-1",
            properties={"$ai_trace_id": kept_trace_id, "step": 2},
        )

        self.assertEqual(len(captured), 2)
        for event in captured:
            self.assertEqual(event["properties"]["$ai_trace_id"], kept_trace_id)
            self.assertEqual(event["properties"]["$ai_trace_sample_rate"], 0.5)

        # Emit multiple events for the dropped trace — all must be dropped
        captured.clear()
        client.capture(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": dropped_trace_id, "step": 1},
        )
        client.capture(
            "$ai_span",
            distinct_id="user-1",
            properties={"$ai_trace_id": dropped_trace_id, "step": 2},
        )

        self.assertEqual(len(captured), 0)

    def test_before_send_sees_only_kept_events_with_sample_rate(self):
        dropped_trace_id = next(
            f"trace-bs-{i}"
            for i in range(100)
            if not _should_sample_ai_trace(f"trace-bs-{i}", 0.5)
        )
        kept_trace_id = next(
            f"trace-bs-{i}"
            for i in range(100)
            if _should_sample_ai_trace(f"trace-bs-{i}", 0.5)
        )

        before_send_calls = []

        def my_before_send(event):
            before_send_calls.append(event)
            return event

        record_sync_capture_sends(self)
        client = self._create_client(
            ai_trace_sample_rate=0.5,
            before_send=my_before_send,
        )

        # Dropped trace must never reach before_send
        client.capture(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": dropped_trace_id},
        )
        self.assertEqual(len(before_send_calls), 0)

        # Kept trace reaches before_send and already carries $ai_trace_sample_rate
        client.capture(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": kept_trace_id},
        )
        self.assertEqual(len(before_send_calls), 1)
        self.assertEqual(
            before_send_calls[0]["properties"]["$ai_trace_sample_rate"], 0.5
        )

    def test_rate_one_means_no_sampling(self):
        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=1.0)

        client.capture(
            "$ai_generation",
            distinct_id="user-1",
            properties={"$ai_trace_id": "any-trace-id"},
        )
        self.assertEqual(len(captured), 1)
        self.assertNotIn("$ai_trace_sample_rate", captured[0].get("properties", {}))

    def test_async_client_sampling_parity(self):
        async def run_async():
            # 1. AsyncClient with rate=0.0 drops traced $ai_* events
            async with AsyncClient(
                "phc_test", send=False, ai_trace_sample_rate=0.0
            ) as client_zero:
                res_drop = client_zero.capture_ai(
                    "$ai_generation",
                    distinct_id="user-1",
                    properties={"$ai_trace_id": "trace-1"},
                )
                self.assertIsNone(res_drop)

                res_imm_drop = await client_zero.capture_ai_immediate(
                    "$ai_generation",
                    distinct_id="user-1",
                    properties={"$ai_trace_id": "trace-1"},
                )
                self.assertIsNone(res_imm_drop)

            # 2. AsyncClient honors POSTHOG_AI_TRACE_SAMPLE_RATE env var
            with patch.dict(os.environ, {AI_TRACE_SAMPLE_RATE_ENV_VAR: "0.0"}):
                async with AsyncClient("phc_test", send=False) as client_env:
                    self.assertEqual(client_env.ai_trace_sample_rate, 0.0)
                    res_env_drop = client_env.capture_ai(
                        "$ai_generation",
                        distinct_id="user-1",
                        properties={"$ai_trace_id": "trace-1"},
                    )
                    self.assertIsNone(res_env_drop)

            # 3. AsyncClient with rate=1.0 keeps events and returns UUID
            async with AsyncClient(
                "phc_test", send=False, ai_trace_sample_rate=1.0
            ) as client_one:
                res_keep = client_one.capture_ai(
                    "$ai_generation",
                    distinct_id="user-1",
                    properties={"$ai_trace_id": "trace-1"},
                )
                self.assertIsInstance(res_keep, str)

        asyncio.run(run_async())

    def test_multi_item_positive_control_fixture(self):
        """Verifies item order, parity, and trace-level sampling across heterogeneous events."""
        captured = record_sync_capture_sends(self)
        client = self._create_client(ai_trace_sample_rate=0.5)

        kept_trace_id = next(
            f"ctrl-trace-{i}"
            for i in range(100)
            if _should_sample_ai_trace(f"ctrl-trace-{i}", 0.5)
        )
        dropped_trace_id = next(
            f"ctrl-trace-{i}"
            for i in range(100)
            if not _should_sample_ai_trace(f"ctrl-trace-{i}", 0.5)
        )

        test_events = [
            (
                "custom_login",
                "user-1",
                {"$ai_trace_id": dropped_trace_id},
            ),  # Non-AI, kept
            ("$ai_generation", "user-1", {"$ai_trace_id": kept_trace_id}),  # AI kept
            (
                "$ai_generation",
                "user-2",
                {"$ai_trace_id": dropped_trace_id},
            ),  # AI dropped
            ("$ai_metric", "user-3", {}),  # AI without trace id, kept
            ("$pageview", "user-4", {"$current_url": "/docs"}),  # Non-AI, kept
        ]

        results = []
        for ev, uid, props in test_events:
            ret = client.capture(ev, distinct_id=uid, properties=props)
            results.append((ev, ret is not None))

        self.assertEqual(
            results,
            [
                ("custom_login", True),
                ("$ai_generation", True),
                ("$ai_generation", False),
                ("$ai_metric", True),
                ("$pageview", True),
            ],
        )

        # Captured list preserves exact relative order of admitted events
        self.assertEqual(len(captured), 4)
        self.assertEqual(
            [e["event"] for e in captured],
            ["custom_login", "$ai_generation", "$ai_metric", "$pageview"],
        )
        self.assertEqual(captured[1]["properties"]["$ai_trace_sample_rate"], 0.5)
        self.assertNotIn("$ai_trace_sample_rate", captured[0].get("properties", {}))
        self.assertNotIn("$ai_trace_sample_rate", captured[2].get("properties", {}))
        self.assertNotIn("$ai_trace_sample_rate", captured[3].get("properties", {}))
