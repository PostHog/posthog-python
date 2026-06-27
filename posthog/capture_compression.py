import logging
import os
from enum import Enum
from typing import Optional, Union

log = logging.getLogger("posthog")

CAPTURE_COMPRESSION_ENV_VAR = "POSTHOG_CAPTURE_COMPRESSION"


class CaptureCompression(str, Enum):
    """Selects the request-body compression for capture-v1 uploads.

    Only honored when ``capture_mode`` is ``V1``; the legacy ``/batch/`` path
    keeps using its own ``gzip`` flag. ``NONE`` sends the body uncompressed.
    ``GZIP`` and ``DEFLATE`` (zlib, RFC 1950) are both stdlib / zero-dependency
    and map to the matching ``Content-Encoding`` token the v1 server decodes
    (``br``/``zstd`` are accepted by the server too but need extra dependencies,
    so they are intentionally left out for now). Inheriting from ``str`` keeps
    the members comparable to and serializable as their token values.
    """

    NONE = "none"
    GZIP = "gzip"
    DEFLATE = "deflate"


# Accepted spellings for both the kwarg and the env var. ``identity`` mirrors
# the HTTP token for "no encoding".
_ALIASES: dict[str, CaptureCompression] = {
    "none": CaptureCompression.NONE,
    "identity": CaptureCompression.NONE,
    "gzip": CaptureCompression.GZIP,
    "deflate": CaptureCompression.DEFLATE,
}


def _coerce_explicit(
    value: Union[CaptureCompression, str],
) -> CaptureCompression:
    """Normalize an explicitly-supplied compression to a ``CaptureCompression``.

    An explicit but unrecognized value is a programming error, so it raises
    ``ValueError`` rather than silently defaulting (unlike the env var, which is
    operator-supplied and defaults defensively).
    """
    if isinstance(value, CaptureCompression):
        return value
    if isinstance(value, str):
        resolved = _ALIASES.get(value.strip().lower())
        if resolved is not None:
            return resolved
    raise ValueError(
        f"invalid capture_compression {value!r}; expected a CaptureCompression "
        f"or one of {sorted(_ALIASES)}"
    )


def resolve_capture_compression(
    capture_compression: Optional[Union[CaptureCompression, str]] = None,
    *,
    gzip_fallback: bool = False,
) -> CaptureCompression:
    """Resolve the effective v1 compression.

    Precedence: explicit ``capture_compression`` argument >
    ``POSTHOG_CAPTURE_COMPRESSION`` env var > the legacy ``gzip`` flag
    (``GZIP`` when set) > ``NONE``. An unrecognized env value logs a warning and
    falls back to the ``gzip`` flag, so a typo never silently changes encoding.
    """
    if capture_compression is not None:
        return _coerce_explicit(capture_compression)

    fallback = CaptureCompression.GZIP if gzip_fallback else CaptureCompression.NONE

    raw = os.environ.get(CAPTURE_COMPRESSION_ENV_VAR)
    if raw is None or raw.strip() == "":
        return fallback

    resolved = _ALIASES.get(raw.strip().lower())
    if resolved is None:
        log.warning(
            "Unrecognized %s=%r; falling back to %s. Expected one of %s.",
            CAPTURE_COMPRESSION_ENV_VAR,
            raw,
            fallback.value,
            sorted(_ALIASES),
        )
        return fallback
    return resolved
