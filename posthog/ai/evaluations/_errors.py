from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal


class EvaluationAPIError(Exception):
    """An evaluation API request did not produce a confirmed acknowledgment.

    ``persistence`` is ``"rejected"`` for a definite rejection, ``"unknown"``
    when an attempt may have persisted, or ``"not_sent"`` before sending.
    The structured response retains validation paths, conflict counts, and other
    server details. Inspect these attributes explicitly: exception messages omit
    response content to avoid logging evaluation data or credentials.

    Experiment creation errors expose the prepared declaration in ``submission``
    so callers can retry using the same identity and content.
    """

    def __init__(
        self,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: Any = None,
        attr: str | None = None,
        errors: Any = None,
        response: dict[str, Any] | None = None,
        retry_after: float | None = None,
        persistence: Literal["rejected", "unknown", "not_sent"] = "unknown",
        submission: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.detail = deepcopy(detail)
        self.attr = attr
        self.errors = deepcopy(errors)
        self.response = deepcopy(response)
        self.retry_after = retry_after
        self.persistence = persistence
        self.submission = deepcopy(submission)
        http_status = f"HTTP {status}" if status is not None else "no acknowledgment"
        super().__init__(f"Offline evaluation request failed ({http_status}).")
