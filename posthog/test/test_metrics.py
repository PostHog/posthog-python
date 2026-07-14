import gzip
import json
import math
from unittest import mock

import pytest

from posthog.client import Client
from posthog.metrics_capture import DEFAULT_HISTOGRAM_BOUNDS
from posthog.version import VERSION

FAKE_API_KEY = "phc_test_key"


@pytest.fixture
def client():
    c = Client(FAKE_API_KEY, host="https://us.example.com", sync_mode=True)
    yield c
    c.metrics.reset()


def mock_session(status_code=200):
    session = mock.Mock()
    session.post.return_value = mock.Mock(status_code=status_code)
    return session


def sent_payload(session):
    """Decode the gzipped OTLP/JSON body of the last send through the mocked session."""
    args, kwargs = session.post.call_args
    body = kwargs.get("data")
    return json.loads(gzip.decompress(body).decode("utf-8"))


def flush_and_capture(client):
    """Flush metrics with the HTTP boundary mocked; returns (payload, url, kwargs) of the send."""
    session = mock_session()
    with mock.patch("posthog.metrics_capture._get_session", return_value=session):
        client.metrics.flush()
        if not session.post.called:
            return None, None, None
        args, kwargs = session.post.call_args
        return sent_payload(session), args[0] if args else kwargs.get("url"), kwargs


def metrics_from(payload):
    return payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]


class TestMetricsAggregation:
    def test_count_burst_folds_to_one_delta_data_point(self, client):
        # The whole point of pre-aggregation: 1000 count() calls must produce ONE data point
        # whose value is the sum, marked delta + monotonic so the ingest diffs nothing.
        for _ in range(1000):
            client.metrics.count("jobs.processed")
        client.metrics.count("jobs.processed", 5)

        payload, url, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        assert metric["name"] == "jobs.processed"
        assert metric["sum"]["aggregationTemporality"] == 1
        assert metric["sum"]["isMonotonic"] is True
        (dp,) = metric["sum"]["dataPoints"]
        assert dp["asDouble"] == 1005.0
        assert url == "https://us.example.com/i/v1/metrics?token=phc_test_key"

    def test_gauge_keeps_last_value(self, client):
        client.metrics.gauge("queue.depth", 10)
        client.metrics.gauge("queue.depth", 3)

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["gauge"]["dataPoints"]
        assert dp["asDouble"] == 3.0
        # Gauges are instantaneous: no window start on the data point (matches posthog-js).
        assert "startTimeUnixNano" not in dp

    def test_histogram_wire_shape(self, client):
        # Pins the exact OTLP/JSON encoding the ingest's deserializer requires: nano timestamps
        # as strings, but count/bucketCounts as plain JSON numbers (string-encoded u64s are
        # silently dropped upstream — opentelemetry-rust#3328).
        client.metrics.histogram("job.duration", 3, unit="ms")
        client.metrics.histogram("job.duration", 40, unit="ms")
        client.metrics.histogram("job.duration", 99999, unit="ms")

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        assert metric["unit"] == "ms"
        assert metric["histogram"]["aggregationTemporality"] == 1
        (dp,) = metric["histogram"]["dataPoints"]
        assert dp["count"] == 3
        assert dp["sum"] == 100042.0
        assert dp["min"] == 3.0
        assert dp["max"] == 99999.0
        assert dp["explicitBounds"] == DEFAULT_HISTOGRAM_BOUNDS
        assert isinstance(dp["count"], int)
        assert all(isinstance(c, int) for c in dp["bucketCounts"])
        assert len(dp["bucketCounts"]) == len(DEFAULT_HISTOGRAM_BOUNDS) + 1
        assert sum(dp["bucketCounts"]) == 3
        # 3 → first bucket with bound >= 3 (index 1: bound 5); 99999 → overflow bucket.
        assert dp["bucketCounts"][1] == 1
        assert dp["bucketCounts"][-1] == 1
        assert isinstance(dp["timeUnixNano"], str)
        assert isinstance(dp["startTimeUnixNano"], str)

    def test_attribute_sets_split_series_and_order_does_not(self, client):
        client.metrics.count(
            "http.requests", 1, attributes={"route": "/a", "status": "200"}
        )
        client.metrics.count(
            "http.requests", 1, attributes={"status": "200", "route": "/a"}
        )
        client.metrics.count(
            "http.requests", 1, attributes={"route": "/b", "status": "200"}
        )

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        points = metric["sum"]["dataPoints"]
        assert len(points) == 2
        values = sorted(dp["asDouble"] for dp in points)
        assert values == [1.0, 2.0]

    def test_attribute_value_otlp_encoding(self, client):
        # bool must encode as boolValue, not intValue — Python bool is an int subclass, so a
        # naive isinstance(int) check first silently miscodes True as 1.
        client.metrics.count(
            "encoded", 1, attributes={"s": "x", "b": True, "i": 7, "f": 1.5}
        )

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        by_key = {attr["key"]: attr["value"] for attr in dp["attributes"]}
        assert by_key["s"] == {"stringValue": "x"}
        assert by_key["b"] == {"boolValue": True}
        assert by_key["i"] == {"intValue": 7}
        assert by_key["f"] == {"doubleValue": 1.5}


class TestMetricsGuardrails:
    def test_series_cap_drops_new_series_not_existing(self, client):
        capped = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics={"max_series_per_flush": 2},
        )
        capped.metrics.count("m", 1, attributes={"k": "a"})
        capped.metrics.count("m", 1, attributes={"k": "b"})
        capped.metrics.count(
            "m", 1, attributes={"k": "c"}
        )  # new series past cap: dropped
        capped.metrics.count(
            "m", 1, attributes={"k": "a"}
        )  # existing series: still folds

        payload, _, _ = flush_and_capture(capped)
        capped.metrics.reset()

        (metric,) = metrics_from(payload)
        points = metric["sum"]["dataPoints"]
        assert len(points) == 2
        assert sorted(dp["asDouble"] for dp in points) == [1.0, 2.0]

    @pytest.mark.parametrize(
        "record",
        [
            lambda m: m.count("bad", -1),  # counters are monotonic
            lambda m: m.count("bad", math.nan),
            lambda m: m.gauge("bad", math.inf),
            lambda m: m.count("", 1),  # empty name
        ],
    )
    def test_invalid_samples_dropped(self, client, record):
        record(client.metrics)

        payload, _, _ = flush_and_capture(client)

        assert payload is None  # nothing aggregated, nothing sent

    def test_disabled_client_records_nothing(self):
        disabled = Client(FAKE_API_KEY, host="https://us.example.com", disabled=True)
        disabled.metrics.count("m", 1)

        payload, _, _ = flush_and_capture(disabled)

        assert payload is None


class TestMetricsDelivery:
    def test_resource_attributes_layer_sdk_keys_over_user_keys(self):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics={
                "service_name": "billing-worker",
                "environment": "production",
                # A stray user key must not clobber SDK attribution.
                "resource_attributes": {
                    "telemetry.sdk.name": "spoofed",
                    "team": "billing",
                },
            },
        )
        c.metrics.count("m", 1)

        payload, _, _ = flush_and_capture(c)
        c.metrics.reset()

        attrs = {
            attr["key"]: attr["value"]
            for attr in payload["resourceMetrics"][0]["resource"]["attributes"]
        }
        assert attrs["service.name"] == {"stringValue": "billing-worker"}
        assert attrs["deployment.environment"] == {"stringValue": "production"}
        assert attrs["team"] == {"stringValue": "billing"}
        assert attrs["telemetry.sdk.name"] == {"stringValue": "posthog-python"}
        assert attrs["telemetry.sdk.version"] == {"stringValue": VERSION}
        scope = payload["resourceMetrics"][0]["scopeMetrics"][0]["scope"]
        assert scope == {"name": "posthog-python", "version": VERSION}

    def test_transient_failure_merges_window_back(self, client):
        # A 5xx must not lose the window: the counts ride the next flush, summed with new samples.
        client.metrics.count("m", 3)
        with mock.patch(
            "posthog.metrics_capture._get_session", return_value=mock_session(503)
        ):
            client.metrics.flush()

        client.metrics.count("m", 4)
        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        assert dp["asDouble"] == 7.0

    def test_shutdown_flushes_pending_metrics(self):
        c = Client(FAKE_API_KEY, host="https://us.example.com", sync_mode=True)
        c.metrics.count("m", 1)

        session = mock_session()
        with mock.patch("posthog.metrics_capture._get_session", return_value=session):
            c.shutdown()

        assert session.post.called
        payload = sent_payload(session)
        (metric,) = metrics_from(payload)
        assert metric["name"] == "m"


class TestMetricsCrashSafety:
    # A telemetry SDK must never raise into the host application — these inputs all
    # crashed the capture hot path before the series key became JSON-based and
    # _capture gained validation (QA findings, reproduced).

    @pytest.mark.parametrize(
        "attributes",
        [
            {"tags": ["a", "b"]},  # unhashable value
            {"nested": {"k": "v"}},  # unhashable value
            {"a": 1, 2: "x"},  # unsortable mixed-type keys
        ],
    )
    def test_hostile_attributes_do_not_raise(self, client, attributes):
        client.metrics.count("hostile", 1, attributes=attributes)  # must not raise

    def test_non_string_attribute_keys_stringify_on_the_wire(self, client):
        # Series identity str()s keys, but the wire must too — OTLP KeyValue.key is a
        # string field and strict decoders reject numeric keys.
        client.metrics.count("m", 1, attributes={"a": 1, 2: "x"})

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        keys = {attr["key"] for attr in dp["attributes"]}
        assert keys == {"a", "2"}
        assert all(isinstance(attr["key"], str) for attr in dp["attributes"])

    def test_list_attribute_records_as_array_value(self, client):
        client.metrics.count("arr", 1, attributes={"tags": ["a", "b"]})

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        (attr,) = dp["attributes"]
        assert attr["value"] == {
            "arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}
        }

    @pytest.mark.parametrize(
        "hook",
        [
            lambda s: True,  # truthy non-dict return
            lambda s: {**s, "type": "guage"},  # typo'd type must not fold as histogram
        ],
    )
    def test_misbehaving_before_send_drops_sample(self, hook):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics={"before_send": hook},
        )
        c.metrics.count("m", 1)

        payload, _, _ = flush_and_capture(c)
        c.metrics.reset()

        assert payload is None

    def test_bool_and_int_attribute_values_are_distinct_series(self, client):
        # Python True == 1 collapsed these into one series with a tuple-based key.
        client.metrics.count("m", 1, attributes={"k": True})
        client.metrics.count("m", 1, attributes={"k": 1})

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        assert len(metric["sum"]["dataPoints"]) == 2

    def test_none_attribute_values_dropped_before_keying(self, client):
        # A None-valued attribute split the series key but was stripped from the wire,
        # emitting two indistinguishable data points.
        client.metrics.count("m", 1, attributes={"k": None})
        client.metrics.count("m", 1)

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        assert dp["asDouble"] == 2.0


class TestMetricsEncodingParity:
    # The two SDKs must emit byte-identical AnyValues for the same logical input.

    @pytest.mark.parametrize(
        "value,expected",
        [
            (
                math.inf,
                {"stringValue": "Infinity"},
            ),  # proto3 JSON literal, not Python's "inf"
            (-math.inf, {"stringValue": "-Infinity"}),
            (math.nan, {"stringValue": "NaN"}),
            (
                2.0,
                {"intValue": 2},
            ),  # integral floats encode as intValue, matching JS Number.isInteger
            (2.5, {"doubleValue": 2.5}),
        ],
    )
    def test_any_value_encoding_matches_js(self, client, value, expected):
        client.metrics.count("enc", 1, attributes={"v": value})

        payload, _, _ = flush_and_capture(client)

        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        (attr,) = dp["attributes"]
        assert attr["value"] == expected


class TestMetricsFailureHandling:
    def test_fork_drops_inherited_window_and_rearms(self, client):
        # A forked child inherits a dead timer thread and the parent's window; without the
        # PID guard it never flushes again and duplicates the parent's samples.
        client.metrics.count("m", 1)
        assert client.metrics._flush_timer is not None
        client.metrics._pid -= 1  # simulate being in a fork child

        client.metrics.count("m", 5)

        payload, _, _ = flush_and_capture(client)
        (metric,) = metrics_from(payload)
        (dp,) = metric["sum"]["dataPoints"]
        assert (
            dp["asDouble"] == 5.0
        )  # inherited window dropped, only the child's sample remains

    def test_merge_back_respects_series_cap(self):
        c = Client(
            FAKE_API_KEY,
            host="https://us.example.com",
            sync_mode=True,
            metrics={"max_series_per_flush": 2},
        )
        c.metrics.count("m", 1, attributes={"k": "a"})
        c.metrics.count("m", 1, attributes={"k": "b"})
        with mock.patch(
            "posthog.metrics_capture._get_session", return_value=mock_session(503)
        ):
            c.metrics.flush()
        # Attribute churn: two NEW series in the fresh window, then the failed window merges back.
        c.metrics.count("m", 1, attributes={"k": "c"})
        c.metrics.count("m", 1, attributes={"k": "d"})

        assert (
            len(c.metrics._series) <= 2
        )  # cap holds through merge-back; no unbounded backlog
        c.metrics.reset()

    def test_window_dropped_after_consecutive_failures(self, client):
        # A permanently-down endpoint must not buffer forever: after the retry budget the
        # window is dropped loudly instead of growing until a 413 destroys everything.
        client.metrics.count("m", 3)
        with mock.patch(
            "posthog.metrics_capture._get_session", return_value=mock_session(503)
        ):
            for _ in range(4):
                client.metrics.flush()

        payload, _, _ = flush_and_capture(client)

        assert (
            payload is None
        )  # budget exhausted → window dropped, nothing left to send
