"""Shared validation for mutable scorer and experiment tags."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _tag_names(tags: Sequence[str], *, limit: int = 100) -> list[str]:
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
        raise ValueError("tags must be a sequence of strings.")
    if len(tags) > limit:
        raise ValueError(f"tags must contain at most {limit} names.")
    names = list(tags)
    for name in names:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 255:
            raise ValueError(
                "Each tag must be a nonblank string of at most 255 characters."
            )
    # The server trims, lowercases, deduplicates, and sorts names. Preserve the
    # caller's spelling on the wire and return the server's canonical response.
    return names


def _response_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        raise ValueError("Expected a list of tag names.")
    return tuple(value)
