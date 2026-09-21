import logging
from threading import Event, Thread
from typing import Any, Callable, Optional
from urllib.parse import quote

from .request import _get_session, determine_server_host


class _RemoteConfigPoller(Thread):
    def __init__(
        self,
        api_key: str,
        host: str,
        interval: float,
        timeout: float,
        is_enabled: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(name="posthog-remote-config", daemon=True)
        self._api_key = api_key
        self._host = host
        self._interval = interval
        self._timeout = timeout
        self._is_enabled = is_enabled
        self._stopped = Event()
        self._config: Optional[dict[str, Any]] = None

    def stop(self) -> None:
        self._stopped.set()
        self.join()

    def run(self) -> None:
        while not self._stopped.is_set():
            try:
                if self._is_enabled is None or self._is_enabled():
                    config = _fetch_remote_config(
                        self._api_key, self._host, self._timeout
                    )
                    if not self._stopped.is_set():
                        self._config = config
            except Exception:
                # Request exceptions can contain proxy credentials in their URL.
                logging.getLogger("posthog").debug(
                    "Failed to fetch project remote config"
                )
            if self._stopped.wait(self._interval):
                break


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
