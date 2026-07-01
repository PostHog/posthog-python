import logging
import os
from enum import Enum
from typing import Optional, Union

log = logging.getLogger("posthog")

CAPTURE_MODE_ENV_VAR = "POSTHOG_CAPTURE_MODE"


class CaptureMode(str, Enum):
    """Selects the capture wire protocol used for event ingestion.

    ``V0`` is the legacy ``POST /batch/`` endpoint and the default, so upgrading
    is transparent to existing callers. ``V1`` opts into
    ``POST /i/v1/analytics/events`` (Bearer auth, per-event results, partial
    retry). Inheriting from ``str`` keeps the members directly comparable to and
    serializable as their ``"v0"`` / ``"v1"`` values.
    """

    V0 = "v0"
    V1 = "v1"


# Accepted spellings for both the explicit kwarg and the env var. Aliases mirror
# the posthog-go naming (``legacy`` / ``analytics_v1``) so the two SDKs are
# configured with the same vocabulary.
_ALIASES: dict[str, CaptureMode] = {
    "v0": CaptureMode.V0,
    "legacy": CaptureMode.V0,
    "v1": CaptureMode.V1,
    "analytics_v1": CaptureMode.V1,
}


def _coerce_explicit(value: Union[CaptureMode, str]) -> CaptureMode:
    """Normalize an explicitly-supplied capture mode to a ``CaptureMode``.

    Accepts a ``CaptureMode`` or one of the string aliases. An explicit but
    unrecognized value is a programming error, so it raises ``ValueError`` rather
    than silently defaulting (unlike the env var, which is operator-supplied and
    defaults defensively).
    """
    if isinstance(value, CaptureMode):
        return value
    if isinstance(value, str):
        resolved = _ALIASES.get(value.strip().lower())
        if resolved is not None:
            return resolved
    raise ValueError(
        f"invalid capture_mode {value!r}; expected a CaptureMode or one of "
        f"{sorted(_ALIASES)}"
    )


def resolve_capture_mode(
    capture_mode: Optional[Union[CaptureMode, str]] = None,
) -> CaptureMode:
    """Resolve the effective capture mode.

    Precedence: explicit ``capture_mode`` argument > ``POSTHOG_CAPTURE_MODE`` env
    var > ``CaptureMode.V0``. An unrecognized env value logs a warning and falls
    back to ``V0`` so a typo never silently flips the wire protocol.
    """
    if capture_mode is not None:
        return _coerce_explicit(capture_mode)

    raw = os.environ.get(CAPTURE_MODE_ENV_VAR)
    if raw is None or raw.strip() == "":
        return CaptureMode.V0

    resolved = _ALIASES.get(raw.strip().lower())
    if resolved is None:
        log.warning(
            "Unrecognized %s=%r; falling back to %s. Expected one of %s.",
            CAPTURE_MODE_ENV_VAR,
            raw,
            CaptureMode.V0.value,
            sorted(_ALIASES),
        )
        return CaptureMode.V0
    return resolved
