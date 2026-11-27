"""``input_required`` rounds from the 2026-07-28 revision.

A tool that needs more input, such as a confirmation form, answers
``tools/call`` with an ``InputRequiredResult`` and runs when the client retries
with the answers. The round is not a tool call. The SDK records it as
``$mcp_input_required``, and the retry that completes the call is the one
``$mcp_tool_call``.
"""

from typing import Any, Iterable, List, Optional

from ._truncation import truncate_text

INPUT_REQUIRED_ERROR_TYPE = "input_required"
LEGACY_INPUT_REQUIRED_MESSAGE = (
    "The tool asked for more input, which a client on a protocol revision "
    "before 2026-07-28 cannot receive"
)

_INPUT_REQUIRED_RESULT_TYPE = "input_required"
_FIRST_INPUT_REQUIRED_REVISION = "2026-07-28"
_MAX_METHODS = 100
_MAX_METHOD_LENGTH = 256


def input_request_methods(result: Any) -> Optional[List[str]]:
    """The ``method`` of each request an ``input_required`` result carries,
    ordered by key with duplicates kept, or ``None`` when the result is not
    one. Never the requests' content."""
    if isinstance(result, dict):
        result_type = result.get("resultType")
        requests = result.get("inputRequests")
    else:
        result_type = getattr(result, "result_type", None)
        requests = getattr(result, "input_requests", None)
    if result_type != _INPUT_REQUIRED_RESULT_TYPE:
        return None
    if not isinstance(requests, dict):
        return []
    return bound_input_request_methods(
        _method_of(requests[key]) for key in sorted(requests)
    )


def bound_input_request_methods(methods: Iterable[Any]) -> List[str]:
    strings = [method for method in methods if isinstance(method, str)]
    return [
        truncate_text(method, _MAX_METHOD_LENGTH) for method in strings[:_MAX_METHODS]
    ]


def is_legacy_revision(protocol_version: Optional[str]) -> bool:
    """Whether the request declared a revision whose clients cannot receive an
    ``input_required`` result. An unknown revision is not assumed legacy: the
    framework only lets the result through on the modern wire."""
    return (
        isinstance(protocol_version, str)
        and protocol_version < _FIRST_INPUT_REQUIRED_REVISION
    )


def _method_of(request: Any) -> Any:
    if isinstance(request, dict):
        return request.get("method")
    return getattr(request, "method", None)
