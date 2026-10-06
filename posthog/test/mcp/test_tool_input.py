import pytest

from posthog.mcp import ToolInputOptions, get_tool_input_properties


def test_keeps_declared_names_and_redacts_unknown_names():
    input_value = {
        "id": "private-value",
        "context": "analytics-value",
        "llm_model": "example-model",
        "conversation_id": "example-conversation",
        "person@example.com": True,
    }
    schema = {"type": "object", "properties": {"id": {}, "context": {}}}

    assert get_tool_input_properties(input_value, schema) == {
        "$mcp_input_keys": ["context", "id", "[redacted]"]
    }
    assert input_value["id"] == "private-value"


def test_custom_rule_replaces_the_default_and_keeps_declared_names_first():
    seen = []

    def record(key, details):
        seen.append((key, details["declared"]))
        return key.replace("_", "").isalnum()

    result = get_tool_input_properties(
        {"id": 1, "experiment_id": 1, "person@example.com": 1},
        {"properties": {"id": {}}},
        ToolInputOptions(should_record_input_key=record),
    )

    assert result == {"$mcp_input_keys": ["id", "experiment_id", "[redacted]"]}
    assert ("id", True) in seen
    assert ("experiment_id", False) in seen


@pytest.mark.parametrize(
    ("input_value", "schema", "aliases", "expected"),
    [
        (
            {"experimentId": 1, "experiment_id": 2, "flagKey": "k", "other": 3},
            {"properties": {"id": {}, "key": {}}},
            {"id": ["experimentId", "experiment_id"], "key": ["flagKey"]},
            {
                "$mcp_input_keys": [
                    "experimentId",
                    "experiment_id",
                    "flagKey",
                    "[redacted]",
                ],
                "$mcp_input_aliases_used": ["experimentId:id", "flagKey:key"],
            },
        ),
        (
            {"id": 1, "experiment_id": 2},
            {"properties": {"id": {}}},
            {"id": ["experiment_id"]},
            {"$mcp_input_keys": ["experiment_id", "id"]},
        ),
    ],
)
def test_records_input_aliases(input_value, schema, aliases, expected):
    assert (
        get_tool_input_properties(
            input_value, schema, ToolInputOptions(input_aliases=aliases)
        )
        == expected
    )


def test_alias_telemetry_uses_the_recording_rule():
    result = get_tool_input_properties(
        {"privateAlias": 1},
        {"properties": {"id": {}}},
        ToolInputOptions(
            input_aliases={"id": ["privateAlias"]},
            should_record_input_key=lambda key, _details: key != "privateAlias",
        ),
    )

    assert result == {"$mcp_input_keys": ["[redacted]"]}


def test_first_present_alias_controls_alias_telemetry():
    long_alias = "x" * 65
    result = get_tool_input_properties(
        {long_alias: 1, "validAlias": 2},
        {"properties": {"id": {}}},
        ToolInputOptions(input_aliases={"id": [long_alias, "validAlias"]}),
    )

    assert result == {"$mcp_input_keys": ["validAlias", "[redacted]"]}


def test_resolves_local_root_references():
    schema = {
        "$ref": "#/$defs/Args",
        "$defs": {"Args": {"type": "object", "properties": {"id": {"type": "string"}}}},
    }

    assert get_tool_input_properties({"id": 1, "private@example.com": 2}, schema) == {
        "$mcp_input_keys": ["id", "[redacted]"]
    }


def test_bounds_names_and_fails_closed():
    properties = {f"key{i}": {} for i in range(30)}
    result = get_tool_input_properties(properties, {"properties": properties})
    assert len(result["$mcp_input_keys"]) == 20

    assert get_tool_input_properties(None) == {}
    assert get_tool_input_properties({"x" * 65: 1}, {"properties": {"x" * 65: {}}}) == {
        "$mcp_input_keys": ["[redacted]"]
    }


def test_callback_error_redacts_the_name():
    def fail(_key, _details):
        raise RuntimeError("boom")

    assert get_tool_input_properties(
        {"id": 1},
        {"properties": {"id": {}}},
        ToolInputOptions(should_record_input_key=fail),
    ) == {"$mcp_input_keys": ["[redacted]"]}
