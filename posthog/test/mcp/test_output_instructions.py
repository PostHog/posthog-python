"""The ``structuredContent`` delivery channel for the conversation handle.

Clients that read ``structuredContent`` — which they do whenever a tool declares
an output schema — never see the ``content`` text block carrying the handle, so
the echo rate on schema-declaring tools is 0% without this mirror
(posthog-js ADR-0004). Runs under both MCP SDK majors.
"""

from types import SimpleNamespace

from posthog.mcp._output_instructions import (
    MCP_INSTRUCTIONS_KEY,
    add_instructions_to_output_schema,
    can_declare_output_instructions,
    mirror_instructions_into_structured_content,
)


def _tool(output_schema, name="demo"):
    return SimpleNamespace(name=name, outputSchema=output_schema)


# --- declaration (tools/list half) ---------------------------------------------


def test_declares_key_on_a_plain_object_schema():
    tool = _tool({"type": "object", "properties": {"total": {"type": "integer"}}})

    assert add_instructions_to_output_schema(tool) is True

    props = tool.outputSchema["properties"]
    assert (
        props[MCP_INSTRUCTIONS_KEY]["properties"]["conversation_id"]["type"] == "string"
    )
    # never required — every result predating this change lacks the key
    assert MCP_INSTRUCTIONS_KEY not in tool.outputSchema.get("required", [])
    # the customer's own properties survive
    assert "total" in props


def test_declaration_does_not_mutate_the_original_schema():
    original = {"type": "object", "properties": {"total": {"type": "integer"}}}
    tool = _tool(original)

    add_instructions_to_output_schema(tool)

    assert MCP_INSTRUCTIONS_KEY not in original["properties"]


def test_no_output_schema_is_not_declared():
    tool = _tool(None)
    assert add_instructions_to_output_schema(tool) is False
    assert tool.outputSchema is None


def test_composed_schemas_are_skipped():
    for schema in (
        {"oneOf": [{"type": "object"}]},
        {"allOf": [{"type": "object"}]},
        {"anyOf": [{"type": "object"}]},
        {"$ref": "#/defs/Thing"},
    ):
        assert can_declare_output_instructions(schema) is False
        tool = _tool(schema)
        assert add_instructions_to_output_schema(tool) is False


def test_tool_owning_the_key_is_left_alone():
    schema = {
        "type": "object",
        "properties": {MCP_INSTRUCTIONS_KEY: {"type": "string"}},
    }
    tool = _tool(schema)

    assert add_instructions_to_output_schema(tool) is False
    # customer's declaration untouched
    assert tool.outputSchema["properties"][MCP_INSTRUCTIONS_KEY] == {"type": "string"}


def test_malformed_properties_are_refused_rather_than_raising():
    assert (
        can_declare_output_instructions({"type": "object", "properties": True}) is False
    )
    assert (
        can_declare_output_instructions({"type": "object", "properties": []}) is False
    )
    assert (
        add_instructions_to_output_schema(_tool({"type": "object", "properties": True}))
        is False
    )


def test_snake_case_output_schema_attr_is_supported():
    """MCP SDK 2.x models expose ``output_schema``; 1.x exposes ``outputSchema``."""
    tool = SimpleNamespace(
        name="v2tool", output_schema={"type": "object", "properties": {}}
    )

    assert add_instructions_to_output_schema(tool) is True
    assert MCP_INSTRUCTIONS_KEY in tool.output_schema["properties"]


# --- mirroring (tools/call half) ------------------------------------------------


def test_mirrors_into_a_fastmcp_v1_tuple_result():
    result = ([{"type": "text", "text": "{}"}], {"total": 7})

    mirrored, delivered = mirror_instructions_into_structured_content(result, "conv-1")

    assert delivered is True
    assert mirrored[1][MCP_INSTRUCTIONS_KEY] == {"conversation_id": "conv-1"}
    assert mirrored[1]["total"] == 7
    assert mirrored[0] is result[0]  # content untouched


def test_mirrors_into_a_model_result_both_attr_shapes():
    for attr in ("structuredContent", "structured_content"):
        result = SimpleNamespace(**{attr: {"total": 7}})

        _, delivered = mirror_instructions_into_structured_content(result, "conv-2")

        assert delivered is True
        assert getattr(result, attr)[MCP_INSTRUCTIONS_KEY] == {
            "conversation_id": "conv-2"
        }


def test_mirrors_through_a_serverresult_wrapper():
    inner = SimpleNamespace(structuredContent={"total": 1})
    result = SimpleNamespace(root=inner)

    _, delivered = mirror_instructions_into_structured_content(result, "conv-3")

    assert delivered is True
    assert inner.structuredContent[MCP_INSTRUCTIONS_KEY] == {
        "conversation_id": "conv-3"
    }


def test_mirrors_into_a_dict_result_without_mutating_it():
    result = {"structuredContent": {"total": 7}}

    mirrored, delivered = mirror_instructions_into_structured_content(result, "conv-4")

    assert delivered is True
    assert mirrored["structuredContent"][MCP_INSTRUCTIONS_KEY] == {
        "conversation_id": "conv-4"
    }
    assert MCP_INSTRUCTIONS_KEY not in result["structuredContent"]


def test_no_structured_content_is_not_delivered():
    for result in (
        SimpleNamespace(content=[]),  # content-only tool
        {"content": []},
        ([{"type": "text"}], None),  # tuple with no structured half
        "not a result",
        None,
    ):
        _, delivered = mirror_instructions_into_structured_content(result, "conv-5")
        assert delivered is False


def test_customer_key_wins_over_the_mirror():
    result = SimpleNamespace(structuredContent={MCP_INSTRUCTIONS_KEY: "mine"})

    _, delivered = mirror_instructions_into_structured_content(result, "conv-6")

    assert delivered is False
    assert result.structuredContent[MCP_INSTRUCTIONS_KEY] == "mine"


def test_our_own_declaration_is_recognised_on_a_relisting():
    """Servers that hand back persistent Tool objects re-list the *same* object.
    Reading our own prior declaration as customer-owned would flip ownership to
    False and silently switch the mirror off for schema-reading clients."""
    tool = _tool({"type": "object", "properties": {"total": {"type": "integer"}}})

    assert add_instructions_to_output_schema(tool) is True
    # second listing of the very same object — still ours, still declared
    assert add_instructions_to_output_schema(tool) is True
    assert MCP_INSTRUCTIONS_KEY in tool.outputSchema["properties"]


def test_a_customer_key_is_still_not_claimed_as_ours():
    tool = _tool(
        {
            "type": "object",
            "properties": {MCP_INSTRUCTIONS_KEY: {"type": "string"}},
        }
    )

    assert add_instructions_to_output_schema(tool) is False


def test_mirror_does_not_mutate_a_shared_model_result():
    """A tool may return a cached/shared result object. Pinning one
    conversation's handle onto it would serve that handle to every later caller
    — and, through the handle, collapse unrelated clients into one session."""
    import mcp.types as mcp_types

    # `structuredContent` on SDK 1.x, `structured_content` on 2.x (same wire field).
    def structured(result):
        for attr in ("structuredContent", "structured_content"):
            if hasattr(result, attr):
                return getattr(result, attr)
        raise AssertionError("no structured content attribute")

    shared = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="{}")],
        structuredContent={"total": 7},
    )

    first, delivered = mirror_instructions_into_structured_content(shared, "conv-A")

    assert delivered is True
    assert first is not shared  # a copy, not the caller's object
    assert structured(first)[MCP_INSTRUCTIONS_KEY] == {"conversation_id": "conv-A"}
    # the shared object the customer owns is untouched, so the next caller is clean
    assert MCP_INSTRUCTIONS_KEY not in structured(shared)

    second, _ = mirror_instructions_into_structured_content(shared, "conv-B")
    assert structured(second)[MCP_INSTRUCTIONS_KEY] == {"conversation_id": "conv-B"}
