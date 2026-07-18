"""Tests for minimal ``$feature_flag_called`` events.

Per the cross-SDK contract, a minimal event is sent iff the server-controlled gate is
on (top-level ``minimalFlagCalledEvents`` in the v2 ``/flags`` response, or top-level
``minimal_flag_called_events`` in the local-evaluation payload) AND the evaluated
flag's ``has_experiment`` is exactly ``False``. The minimal shape is a strict
allowlist applied to the fully-enriched properties dict; any missing signal fails safe
to the full legacy shape.
"""

import unittest
from unittest import mock

from parameterized import parameterized

from posthog.client import _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES, Client
from posthog.request import GetResponse
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.utils import system_context


def _flags_response(has_experiment, gate):
    """Build a v2 ``/flags`` response for ``person-flag``.

    ``has_experiment=None`` omits the per-flag field; ``gate=None`` omits the
    top-level ``minimalFlagCalledEvents`` field, simulating an ungated team or an
    older server.
    """
    metadata = {"id": 23, "version": 42, "payload": "300"}
    if has_experiment is not None:
        metadata["has_experiment"] = has_experiment
    response = {
        "flags": {
            "person-flag": {
                "key": "person-flag",
                "enabled": True,
                "variant": None,
                "reason": {"description": "Matched condition set 1"},
                "metadata": metadata,
            },
        },
        "requestId": "req-1",
        "evaluatedAt": 1640995200000,
    }
    if gate is not None:
        response["minimalFlagCalledEvents"] = gate
    return response


def _local_flag_definition(has_experiment):
    flag = {
        "id": 1,
        "name": "Beta Feature",
        "key": "person-flag",
        "active": True,
        "filters": {
            "groups": [{"properties": [], "rollout_percentage": 100}],
            "payloads": {"true": "300"},
        },
    }
    if has_experiment is not None:
        flag["has_experiment"] = has_experiment
    return flag


class _CapturedEventsMixin:
    """Builds a non-sending client whose fully-enriched events are captured via
    ``before_send``, so tests assert the exact wire shape after every enrichment
    step (system context, super properties, $lib, ...)."""

    def _make_client(self, **kwargs):
        captured = []

        def before_send(msg):
            captured.append(msg)
            return msg

        client = Client(
            FAKE_TEST_API_KEY,
            send=False,
            before_send=before_send,
            super_properties={"app_version": "1.2.3"},
            **kwargs,
        )
        return client, captured

    def _flag_called_properties(self, captured, index=0):
        events = [m for m in captured if m["event"] == "$feature_flag_called"]
        assert events, "no $feature_flag_called event captured"
        return events[index]["properties"]


class TestMinimizationViaFlagsResponse(_CapturedEventsMixin, unittest.TestCase):
    """Gate sourced from the top-level ``minimalFlagCalledEvents`` field of the v2
    ``/flags`` response."""

    @mock.patch("posthog.client.flags")
    def test_gated_non_experiment_flag_sends_exactly_the_allowlist(self, patch_flags):
        patch_flags.return_value = _flags_response(has_experiment=False, gate=True)
        client, captured = self._make_client()

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._flag_called_properties(captured)
        # The event shape is exactly the allowlist intersection of what the full
        # event would have carried: no $feature/<key>, no payload, no super
        # properties. Static platform/runtime identity (system_context()) survives.
        self.assertEqual(
            set(properties),
            {
                "$feature_flag",
                "$feature_flag_response",
                "$feature_flag_has_experiment",
                "$feature_flag_id",
                "$feature_flag_version",
                "$feature_flag_reason",
                "$feature_flag_request_id",
                "$feature_flag_evaluated_at",
                "locally_evaluated",
                "$lib",
                "$lib_version",
                "$is_server",
                "$geoip_disable",
            }
            | set(system_context()),
        )
        self.assertLessEqual(set(properties), _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES)
        self.assertIs(properties["$is_server"], True)
        self.assertEqual(properties["$feature_flag"], "person-flag")
        self.assertEqual(properties["$feature_flag_response"], True)
        self.assertIs(properties["$feature_flag_has_experiment"], False)
        self.assertEqual(properties["$feature_flag_id"], 23)
        self.assertEqual(properties["$feature_flag_version"], 42)
        self.assertEqual(properties["$feature_flag_reason"], "Matched condition set 1")
        self.assertEqual(properties["$feature_flag_request_id"], "req-1")
        self.assertEqual(properties["$feature_flag_evaluated_at"], 1640995200000)

    @mock.patch("posthog.client.flags")
    def test_gated_non_experiment_flag_with_groups_keeps_group_context(
        self, patch_flags
    ):
        # $groups and $process_person_profile are "correctness-required" allowlist
        # entries: this pins that they actually survive minimization rather than
        # just being present in the allowlist with no test ever exercising them.
        patch_flags.return_value = _flags_response(has_experiment=False, gate=True)
        client, captured = self._make_client()

        client.get_feature_flag_result(
            "person-flag", "some-distinct-id", groups={"organization": "org-1"}
        )

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$groups"], {"organization": "org-1"})
        self.assertLessEqual(set(properties), _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES)

    @mock.patch("posthog.client.flags")
    def test_remote_evaluation_uses_this_responses_gate_not_a_concurrent_update(
        self, patch_flags
    ):
        # This response's gate is off. Simulate a concurrent poller refresh (or
        # another /flags call on another thread) flipping the client-wide gate on,
        # in the gap between this response landing and the event being captured.
        # The single-flag path must shape the event from this response's own gate,
        # not whatever the client-wide attribute reads at capture time.
        patch_flags.return_value = _flags_response(has_experiment=False, gate=False)
        client, captured = self._make_client()

        original_get_details = client._get_feature_flag_details_from_server

        def get_details_then_concurrent_gate_flip(*args, **kwargs):
            result = original_get_details(*args, **kwargs)
            client._minimal_flag_called_events = True
            return result

        client._get_feature_flag_details_from_server = (
            get_details_then_concurrent_gate_flip
        )

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._flag_called_properties(captured)
        # Had the single-flag path re-read the (now-flipped) client-wide gate at
        # capture time, this would have minimized instead.
        self.assertEqual(properties["$feature/person-flag"], True)
        self.assertIn("$python_version", properties)

    @mock.patch("posthog.client.flags")
    def test_gated_experiment_flag_sends_full_event(self, patch_flags):
        patch_flags.return_value = _flags_response(has_experiment=True, gate=True)
        client, captured = self._make_client()

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/person-flag"], True)
        self.assertEqual(properties["$feature_flag_payload"], 300)
        self.assertIn("$python_version", properties)
        self.assertEqual(properties["app_version"], "1.2.3")
        self.assertIs(properties["$feature_flag_has_experiment"], True)

    @parameterized.expand(
        [
            ("gate_absent", None),
            ("gate_false", False),
            ("gate_not_a_bool", "true"),
        ]
    )
    @mock.patch("posthog.client.flags")
    def test_without_a_true_gate_sends_full_event(self, _name, gate, patch_flags):
        patch_flags.return_value = _flags_response(has_experiment=False, gate=gate)
        client, captured = self._make_client()

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        self.assertIs(client._minimal_flag_called_events, False)
        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/person-flag"], True)
        self.assertEqual(properties["$feature_flag_payload"], 300)
        self.assertIn("$python_version", properties)
        self.assertEqual(properties["app_version"], "1.2.3")
        # The experiment signal still rides along when the server reports it.
        self.assertIs(properties["$feature_flag_has_experiment"], False)

    @mock.patch("posthog.client.flags")
    def test_gated_flag_without_has_experiment_sends_full_event(self, patch_flags):
        # Gate on, but the per-flag signal is missing (older deployment): fail safe
        # to the full shape, and don't fabricate the has_experiment property.
        patch_flags.return_value = _flags_response(has_experiment=None, gate=True)
        client, captured = self._make_client()

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/person-flag"], True)
        self.assertEqual(properties["$feature_flag_payload"], 300)
        self.assertNotIn("$feature_flag_has_experiment", properties)

    @mock.patch("posthog.client.flags")
    def test_gate_persists_and_follows_the_latest_response(self, patch_flags):
        patch_flags.side_effect = [
            _flags_response(has_experiment=False, gate=True),
            _flags_response(has_experiment=False, gate=None),
        ]
        client, captured = self._make_client()

        client.get_feature_flag_result("person-flag", "user-1")
        self.assertIs(client._minimal_flag_called_events, True)

        # A later response without the field flips the gate off and full events resume.
        client.get_feature_flag_result("person-flag", "user-2")
        self.assertIs(client._minimal_flag_called_events, False)

        minimal_properties = self._flag_called_properties(captured, index=0)
        full_properties = self._flag_called_properties(captured, index=1)
        self.assertNotIn("$feature/person-flag", minimal_properties)
        self.assertEqual(full_properties["$feature/person-flag"], True)


class TestMinimizationViaLocalEvaluationPayload(
    _CapturedEventsMixin, unittest.TestCase
):
    """Gate sourced from the top-level ``minimal_flag_called_events`` key of the
    local-evaluation payload, persisted alongside the polled flag definitions."""

    def _load_definitions(self, client, patch_get, *responses):
        patch_get.side_effect = list(responses)
        client.load_feature_flags()

    def _definitions_payload(self, has_experiment, gate):
        data = {
            "flags": [_local_flag_definition(has_experiment)],
            "group_type_mapping": {},
            "cohorts": {},
        }
        if gate is not None:
            data["minimal_flag_called_events"] = gate
        return GetResponse(data=data, etag='"etag-1"')

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_gated_non_experiment_flag_sends_exactly_the_allowlist(
        self, patch_get, _patch_poller
    ):
        client, captured = self._make_client(personal_api_key="personal-key")
        self._load_definitions(
            client,
            patch_get,
            self._definitions_payload(has_experiment=False, gate=True),
        )

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        self.assertIs(client._minimal_flag_called_events, True)
        properties = self._flag_called_properties(captured)
        self.assertEqual(
            set(properties),
            {
                "$feature_flag",
                "$feature_flag_response",
                "$feature_flag_has_experiment",
                "locally_evaluated",
                "$lib",
                "$lib_version",
                "$is_server",
                "$geoip_disable",
            }
            | set(system_context()),
        )
        self.assertLessEqual(set(properties), _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES)
        self.assertEqual(properties["$feature_flag_response"], True)
        self.assertIs(properties["locally_evaluated"], True)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_gated_experiment_flag_sends_full_event(self, patch_get, _patch_poller):
        client, captured = self._make_client(personal_api_key="personal-key")
        self._load_definitions(
            client,
            patch_get,
            self._definitions_payload(has_experiment=True, gate=True),
        )

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/person-flag"], True)
        self.assertEqual(properties["$feature_flag_payload"], 300)
        self.assertIn("$python_version", properties)
        self.assertIs(properties["$feature_flag_has_experiment"], True)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_gate_absent_from_payload_sends_full_event(self, patch_get, _patch_poller):
        client, captured = self._make_client(personal_api_key="personal-key")
        self._load_definitions(
            client,
            patch_get,
            self._definitions_payload(has_experiment=False, gate=None),
        )

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        self.assertIs(client._minimal_flag_called_events, False)
        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/person-flag"], True)
        self.assertEqual(properties["$feature_flag_payload"], 300)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_gate_survives_not_modified_polls(self, patch_get, _patch_poller):
        client, captured = self._make_client(personal_api_key="personal-key")
        self._load_definitions(
            client,
            patch_get,
            self._definitions_payload(has_experiment=False, gate=True),
            GetResponse(data=None, etag='"etag-1"', not_modified=True),
        )

        # A 304 poll keeps the cached definitions and the gate.
        client._load_feature_flags()
        self.assertIs(client._minimal_flag_called_events, True)

        client.get_feature_flag_result("person-flag", "some-distinct-id")
        properties = self._flag_called_properties(captured)
        self.assertNotIn("$feature/person-flag", properties)
        self.assertNotIn("$feature_flag_payload", properties)


class TestMinimizationViaEvaluateFlagsSnapshot(_CapturedEventsMixin, unittest.TestCase):
    """The ``evaluate_flags()`` snapshot path fires ``$feature_flag_called`` on
    access and must minimize identically to the single-flag path."""

    def _snapshot_response(self, has_experiment, gate):
        metadata = {"id": 2, "version": 23, "payload": '{"key": "value"}'}
        if has_experiment is not None:
            metadata["has_experiment"] = has_experiment
        response = {
            "flags": {
                "variant-flag": {
                    "key": "variant-flag",
                    "enabled": True,
                    "variant": "variant-value",
                    "reason": {"code": "variant", "description": "Matched set 3"},
                    "metadata": metadata,
                },
            },
            "requestId": "request-id-1",
            "evaluatedAt": 1640995200000,
        }
        if gate is not None:
            response["minimalFlagCalledEvents"] = gate
        return response

    @mock.patch("posthog.client.flags")
    def test_gated_non_experiment_flag_sends_exactly_the_allowlist(self, patch_flags):
        patch_flags.return_value = self._snapshot_response(
            has_experiment=False, gate=True
        )
        client, captured = self._make_client()

        flags = client.evaluate_flags("user-1")
        flags.get_flag("variant-flag")

        properties = self._flag_called_properties(captured)
        self.assertEqual(
            set(properties),
            {
                "$feature_flag",
                "$feature_flag_response",
                "$feature_flag_has_experiment",
                "$feature_flag_id",
                "$feature_flag_version",
                "$feature_flag_reason",
                "$feature_flag_request_id",
                "$feature_flag_evaluated_at",
                "locally_evaluated",
                "$lib",
                "$lib_version",
                "$is_server",
                "$geoip_disable",
            }
            | set(system_context()),
        )
        self.assertLessEqual(set(properties), _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES)
        self.assertEqual(properties["$feature_flag_response"], "variant-value")
        self.assertIs(properties["$feature_flag_has_experiment"], False)

    @mock.patch("posthog.client.flags")
    def test_gated_experiment_flag_sends_full_event(self, patch_flags):
        patch_flags.return_value = self._snapshot_response(
            has_experiment=True, gate=True
        )
        client, captured = self._make_client()

        flags = client.evaluate_flags("user-1")
        flags.get_flag("variant-flag")

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/variant-flag"], "variant-value")
        # The evaluate_flags path JSON-parses the payload before attaching it.
        self.assertEqual(properties["$feature_flag_payload"], {"key": "value"})
        self.assertIn("$python_version", properties)
        self.assertIs(properties["$feature_flag_has_experiment"], True)

    @mock.patch("posthog.client.flags")
    def test_ungated_non_experiment_flag_sends_full_event(self, patch_flags):
        patch_flags.return_value = self._snapshot_response(
            has_experiment=False, gate=None
        )
        client, captured = self._make_client()

        flags = client.evaluate_flags("user-1")
        flags.get_flag("variant-flag")

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/variant-flag"], "variant-value")
        self.assertEqual(properties["$feature_flag_payload"], {"key": "value"})
        self.assertEqual(properties["app_version"], "1.2.3")

    @mock.patch("posthog.client.flags")
    def test_snapshot_pins_gate_at_creation_not_at_access(self, patch_flags):
        # The gate is pinned when the snapshot is created: a gate change learned from
        # a LATER response must not reshape events for flags evaluated earlier.
        patch_flags.return_value = self._snapshot_response(
            has_experiment=False, gate=None
        )
        client, captured = self._make_client()

        flags = client.evaluate_flags("user-1")
        # A later response enables the gate before the snapshot is accessed.
        client._minimal_flag_called_events = True
        flags.get_flag("variant-flag")

        properties = self._flag_called_properties(captured)
        self.assertEqual(properties["$feature/variant-flag"], "variant-value")
        self.assertEqual(properties["app_version"], "1.2.3")

    @mock.patch("posthog.client.flags")
    def test_snapshot_keeps_pinned_gate_when_later_response_disables_it(
        self, patch_flags
    ):
        patch_flags.return_value = self._snapshot_response(
            has_experiment=False, gate=True
        )
        client, captured = self._make_client()

        flags = client.evaluate_flags("user-1")
        # A later response disables the gate; this snapshot's evaluation authorized
        # minimization, so its events stay minimal.
        client._minimal_flag_called_events = False
        flags.get_flag("variant-flag")

        properties = self._flag_called_properties(captured)
        self.assertLessEqual(set(properties), _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES)
        self.assertIs(properties["$feature_flag_has_experiment"], False)
