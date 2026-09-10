"""``get_request_headers`` — reading HTTP headers inside a host callback.

Host callbacks receive the SDK's own per-request context under ``extra["ctx"]``,
unchanged. Its shape differs between MCP SDK majors, so header reads go through
this helper instead of a hand-rolled path that silently returns ``None`` on the
other major (the failure mode is invisible: ``identify`` returns nothing and
every event goes out anonymous). Runs under both majors.
"""

from types import SimpleNamespace

from posthog.mcp import get_request_headers


def _ctx(headers):
    return SimpleNamespace(request=SimpleNamespace(headers=headers))


def test_reads_headers_from_a_callback_extra():
    extra = {"session_id": "abc", "ctx": _ctx({"Authorization": "Bearer t0ken"})}

    assert get_request_headers(extra) == {"authorization": "Bearer t0ken"}


def test_keys_are_lowercased():
    extra = {
        "ctx": _ctx({"X-Anthropic-Client": "claude-code", "USER-AGENT": "probe/1"})
    }

    assert get_request_headers(extra) == {
        "x-anthropic-client": "claude-code",
        "user-agent": "probe/1",
    }


def test_accepts_a_raw_context_too():
    """A host holding the context itself shouldn't have to wrap it in a dict."""
    assert get_request_headers(_ctx({"a": "b"})) == {"a": "b"}


def test_starlette_style_headers_are_supported():
    class Headers:
        """Starlette's Headers: already-lowercased, .items() of pairs."""

        def items(self):
            return [("content-type", "application/json"), ("mcp-session-id", "s1")]

    assert get_request_headers({"ctx": _ctx(Headers())}) == {
        "content-type": "application/json",
        "mcp-session-id": "s1",
    }


def test_stdio_and_missing_context_return_none():
    assert get_request_headers({"ctx": _ctx(None)}) is None  # HTTP-less transport
    assert get_request_headers({"ctx": SimpleNamespace(request=None)}) is None
    assert get_request_headers({"session_id": "abc"}) is None  # no ctx at all
    assert get_request_headers(None) is None


def test_never_raises_on_a_hostile_header_object():
    class Exploding:
        def items(self):
            raise RuntimeError("nope")

    assert get_request_headers({"ctx": _ctx(Exploding())}) is None


def test_non_string_header_values_are_skipped():
    extra = {"ctx": _ctx({"good": "yes", "bad": 42, 7: "alsobad"})}

    assert get_request_headers(extra) == {"good": "yes"}
