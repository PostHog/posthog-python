import os
from typing import Optional

__all__ = ["RELEASE_ID_ENV_VAR"]

RELEASE_ID_ENV_VAR = "POSTHOG_RELEASE_ID"


def _resolve_release_id() -> Optional[str]:
    """Resolve the release id reported as ``$release_id`` on every event.

    This is the deploy-time counterpart to injecting ``$release_id`` into a web
    bundle. A Python app has no bundle, so a build tool creates the release with
    ``posthog-cli release resolve`` and launches the app with the printed id in
    ``POSTHOG_RELEASE_ID``. On ``$exception`` events the server then resolves the
    release by a direct id lookup, so no release name or version has to match
    anything the app reports. On other events the id is a plain property that
    ties the event to the release that produced it.

    The value is trimmed and a blank value is treated as unset, so
    ``POSTHOG_RELEASE_ID=`` (or whitespace) never sends an empty ``$release_id``.
    """
    raw = os.environ.get(RELEASE_ID_ENV_VAR)
    if raw is None:
        return None
    return raw.strip() or None
