import pytest

from posthog.tracing._config import (
    DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH,
    DEFAULT_MAX_ATTRIBUTES_PER_SPAN,
    DEFAULT_MAX_EVENTS_PER_SPAN,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    DEFAULT_MAX_EXPORT_BATCH_SIZE,
    DEFAULT_MAX_LIVE_SPANS,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_MAX_SPAN_AGE_SECONDS,
    ResolvedTracesConfig,
    resolve_traces_config,
)


class TestDefaults:
    def test_applies_the_documented_defaults(self):
        assert resolve_traces_config({}) == ResolvedTracesConfig(
            flush_interval=DEFAULT_FLUSH_INTERVAL_SECONDS,
            max_export_batch_size=DEFAULT_MAX_EXPORT_BATCH_SIZE,
            max_queue_size=DEFAULT_MAX_QUEUE_SIZE,
            max_live_spans=DEFAULT_MAX_LIVE_SPANS,
            max_span_age=DEFAULT_MAX_SPAN_AGE_SECONDS,
        )

    def test_leaves_service_name_unset_so_the_encoder_supplies_unknown_service(self):
        assert resolve_traces_config({}).service_name is None

    @pytest.mark.parametrize("config", [None, "nope", 42, ["a"]])
    def test_a_non_dict_config_falls_back_to_defaults(self, config):
        assert resolve_traces_config(config) == resolve_traces_config({})


class TestExplicitValues:
    def test_honours_explicit_values(self):
        resolved = resolve_traces_config(
            {
                "service_name": "api",
                "service_version": "1.2.3",
                "environment": "prod",
                "flush_interval": 2,
                "max_export_batch_size": 100,
                "max_queue_size": 400,
                "max_live_spans": 50,
                "max_span_age": 60,
            }
        )
        assert resolved == ResolvedTracesConfig(
            service_name="api",
            service_version="1.2.3",
            environment="prod",
            flush_interval=2.0,
            max_export_batch_size=100,
            max_queue_size=400,
            max_live_spans=50,
            max_span_age=60.0,
        )

    @pytest.mark.parametrize(
        "value",
        [0, -1, 0.5, 512.7, float("nan"), float("inf"), "512", True, None],
    )
    def test_falls_back_for_an_unusable_batch_size(self, value):
        assert (
            resolve_traces_config(
                {"max_export_batch_size": value}
            ).max_export_batch_size
            == DEFAULT_MAX_EXPORT_BATCH_SIZE
        )

    def test_accepts_a_whole_number_float_batch_size(self):
        assert (
            resolve_traces_config(
                {"max_export_batch_size": 100.0}
            ).max_export_batch_size
            == 100
        )

    def test_an_unusable_knob_keeps_the_rest_of_the_config(self):
        resolved = resolve_traces_config(
            {"service_name": "api", "max_live_spans": float("inf")}
        )
        assert resolved.service_name == "api"
        assert resolved.max_live_spans == DEFAULT_MAX_LIVE_SPANS

    @pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "5", False])
    def test_falls_back_for_an_unusable_flush_interval(self, value):
        assert (
            resolve_traces_config({"flush_interval": value}).flush_interval
            == DEFAULT_FLUSH_INTERVAL_SECONDS
        )

    @pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
    def test_falls_back_for_unusable_live_span_bounds(self, value):
        resolved = resolve_traces_config(
            {"max_live_spans": value, "max_span_age": value}
        )
        assert resolved.max_live_spans == DEFAULT_MAX_LIVE_SPANS
        assert resolved.max_span_age == DEFAULT_MAX_SPAN_AGE_SECONDS

    def test_keeps_the_queue_at_least_as_large_as_the_export_batch(self):
        resolved = resolve_traces_config({"max_export_batch_size": 4096})
        assert resolved.max_queue_size == 4096

    def test_floors_an_explicit_queue_size_at_the_batch_size(self):
        resolved = resolve_traces_config(
            {"max_export_batch_size": 10, "max_queue_size": 3}
        )
        assert resolved.max_queue_size == 10

    def test_ignores_a_non_string_named_field(self):
        assert resolve_traces_config({"service_name": 42}).service_name is None


class TestResourceAttributes:
    def test_lets_otlp_resource_attributes_override_the_named_fields(self):
        resolved = resolve_traces_config(
            {
                "service_name": "named",
                "resource_attributes": {"service.name": "from-attrs", "region": "eu"},
            }
        )
        assert resolved.service_name == "from-attrs"
        assert resolved.resource_attributes == {
            "service.name": "from-attrs",
            "region": "eu",
        }

    def test_attaches_host_attributes_and_lets_user_attributes_override_them(self):
        resolved = resolve_traces_config(
            {"resource_attributes": {"os.name": "Custom"}},
            {"os.name": "Linux", "os.version": "6.1"},
        )
        assert resolved.resource_attributes == {
            "os.name": "Custom",
            "os.version": "6.1",
        }

    def test_ignores_a_non_dict_value(self):
        assert (
            resolve_traces_config({"resource_attributes": ["a"]}).resource_attributes
            == {}
        )

    def test_drops_an_identity_key_that_is_not_a_string(self):
        resolved = resolve_traces_config(
            {
                "service_name": "named",
                "resource_attributes": {
                    "service.name": 42,
                    "deployment.environment": 1,
                },
            }
        )
        assert resolved.service_name == "named"
        assert resolved.environment is None
        assert "service.name" not in resolved.resource_attributes

    def test_keeps_the_readable_attributes_when_one_accessor_raises(self):
        class Explosive(dict):
            def __getitem__(self, key):
                if key == "bad":
                    raise RuntimeError("boom")
                return super().__getitem__(key)

        resolved = resolve_traces_config(
            {"resource_attributes": Explosive(good=1, bad=2)}
        )
        assert resolved.resource_attributes == {"good": 1}


class TestHostileResourceAttributeKeys:
    def test_drops_only_a_key_that_cannot_be_stringified(self):
        class HostileKey:
            def __str__(self):
                raise RuntimeError("no")

        resolved = resolve_traces_config(
            {
                "service_name": "api",
                "resource_attributes": {HostileKey(): 1, "team": "x"},
            }
        )
        assert resolved.service_name == "api"
        assert resolved.resource_attributes["team"] == "x"
        assert all(isinstance(key, str) for key in resolved.resource_attributes)


class TestSpanLimitKnobs:
    def test_defaults_to_opentelemetrys_counts_and_a_finite_value_length(self):
        resolved = resolve_traces_config({})
        assert (
            resolved.max_attributes_per_span == DEFAULT_MAX_ATTRIBUTES_PER_SPAN == 128
        )
        assert resolved.max_events_per_span == DEFAULT_MAX_EVENTS_PER_SPAN == 128
        assert resolved.max_attribute_value_length == DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH
        assert DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH == 8192

    def test_honours_explicit_values(self):
        resolved = resolve_traces_config(
            {
                "max_attributes_per_span": 10,
                "max_events_per_span": 5,
                "max_attribute_value_length": 100,
            }
        )
        assert resolved.max_attributes_per_span == 10
        assert resolved.max_events_per_span == 5
        assert resolved.max_attribute_value_length == 100

    @pytest.mark.parametrize("value", [0, -1, 1.5, "128", None, True])
    def test_an_unusable_value_falls_back_rather_than_dropping_every_span(self, value):
        resolved = resolve_traces_config(
            {
                "max_attributes_per_span": value,
                "max_events_per_span": value,
                "max_attribute_value_length": value,
            }
        )
        assert resolved.max_attributes_per_span == DEFAULT_MAX_ATTRIBUTES_PER_SPAN
        assert resolved.max_events_per_span == DEFAULT_MAX_EVENTS_PER_SPAN
        assert resolved.max_attribute_value_length == DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH


class TestBeforeSpanSendConfig:
    def test_accepts_one_hook_or_a_list(self):
        def hook(span):
            return span

        assert resolve_traces_config({"before_span_send": hook}).before_span_send == (
            hook,
        )
        assert resolve_traces_config(
            {"before_span_send": [hook, hook]}
        ).before_span_send == (hook, hook)

    def test_defaults_to_no_hooks(self):
        assert resolve_traces_config({}).before_span_send == ()

    def test_skips_falsy_entries_silently(self, caplog):
        caplog.set_level("WARNING", logger="posthog")

        def hook(span):
            return span

        resolved = resolve_traces_config({"before_span_send": [None, False, hook]})
        assert resolved.before_span_send == (hook,)
        assert not caplog.records

    def test_ignores_and_warns_about_entries_that_are_not_callable(self, caplog):
        caplog.set_level("WARNING", logger="posthog")

        def hook(span):
            return span

        resolved = resolve_traces_config({"before_span_send": ["scrub", hook]})
        assert resolved.before_span_send == (hook,)
        assert any("1 of 2" in r.getMessage() for r in caplog.records)

    def test_a_hook_whose_truthiness_raises_is_still_resolved(self):
        class Hook:
            def __bool__(self):
                raise RuntimeError("no")

            def __call__(self, span):
                return span

        hook = Hook()
        assert resolve_traces_config({"before_span_send": hook}).before_span_send == (
            hook,
        )
