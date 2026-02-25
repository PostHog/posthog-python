from parameterized import parameterized

from posthog.ai.utils import _get_tokens_source


@parameterized.expand(
    [
        ("no_posthog_properties", {"$ai_input_tokens": 100}, None, "sdk"),
        ("empty_posthog_properties", {"$ai_input_tokens": 100}, {}, "sdk"),
        (
            "unrelated_posthog_properties",
            {"$ai_input_tokens": 100},
            {"foo": "bar"},
            "sdk",
        ),
        (
            "override_input_tokens",
            {"$ai_input_tokens": 100},
            {"$ai_input_tokens": 999},
            "passthrough",
        ),
        (
            "override_output_tokens",
            {"$ai_output_tokens": 50},
            {"$ai_output_tokens": 999},
            "passthrough",
        ),
        (
            "override_total_tokens",
            {"$ai_input_tokens": 100},
            {"$ai_total_tokens": 999},
            "passthrough",
        ),
        (
            "override_cache_read",
            {"$ai_input_tokens": 100},
            {"$ai_cache_read_input_tokens": 500},
            "passthrough",
        ),
        (
            "override_cache_creation",
            {"$ai_input_tokens": 100},
            {"$ai_cache_creation_input_tokens": 200},
            "passthrough",
        ),
        (
            "override_reasoning_tokens",
            {"$ai_input_tokens": 100},
            {"$ai_reasoning_tokens": 300},
            "passthrough",
        ),
        (
            "mixed_override_and_custom",
            {"$ai_input_tokens": 100},
            {"$ai_input_tokens": 999, "custom_key": "value"},
            "passthrough",
        ),
    ]
)
def test_get_tokens_source(name, sdk_tags, posthog_properties, expected):
    result = _get_tokens_source(sdk_tags, posthog_properties)
    assert result == expected
