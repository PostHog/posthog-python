import logging
from datetime import timedelta
from typing import Any, Callable, Optional
from urllib.parse import quote

from .poller import Poller
from .request import _get_session, determine_server_host


class _RemoteConfigPoller(Poller):
    def __init__(
        self,
        api_key: str,
        host: str,
        interval: float,
        timeout: float,
        is_enabled: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(interval=timedelta(seconds=interval), execute=self._refresh)
        self.name = "posthog-remote-config"
        self._api_key = api_key
        self._host = host
        self._timeout = timeout
        self._is_enabled = is_enabled
        self._config: Optional[dict[str, Any]] = None

    def run(self) -> None:
        self._refresh()
        super().run()

    def _refresh(self) -> None:
        if self.stopped.is_set():
            return
        try:
            if self._is_enabled is None or self._is_enabled():
                config = _fetch_remote_config(self._api_key, self._host, self._timeout)
                if not self.stopped.is_set():
                    self._config = config
        except Exception:
            # Request exceptions can contain proxy credentials in their URL.
            logging.getLogger("posthog").debug("Failed to fetch project remote config")


def _fetch_remote_config(
    api_key: str, host: Optional[str], timeout: float
) -> dict[str, Any]:
    base = determine_server_host(host).rstrip("/")
    base = {
        "https://us.i.posthog.com": "https://us-assets.i.posthog.com",
        "https://eu.i.posthog.com": "https://eu-assets.i.posthog.com",
    }.get(base, base)
    url = f"{base}/array/{quote(api_key, safe='')}/config"
    with _get_session().get(url, timeout=timeout) as response:
        response.raise_for_status()
        config = response.json()
    if not isinstance(config, dict):
        raise ValueError("Project remote config must be a JSON object")
    return config
