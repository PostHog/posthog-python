from typing import Any

from . import adapters as adapters, exceptions as exceptions

class Response:
    status_code: int
    ok: bool
    text: str
    headers: dict[str, str]
    def json(self) -> Any: ...

class Session:
    def mount(self, prefix: str, adapter: adapters.HTTPAdapter) -> None: ...
    def close(self) -> None: ...
    def post(
        self,
        url: str,
        *,
        data: str | bytes,
        headers: dict[str, str],
        timeout: int,
    ) -> Response: ...
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int | None = ...,
    ) -> Response: ...
