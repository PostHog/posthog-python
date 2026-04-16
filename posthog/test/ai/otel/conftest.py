from unittest.mock import MagicMock


def make_span(name: str = "test", attributes: dict | None = None) -> MagicMock:
    span = MagicMock()
    span.name = name
    span.attributes = attributes or {}
    return span
