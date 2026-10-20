from typing import Any

from . import adapters as adapters, auth as auth, exceptions as exceptions

class PreparedRequest:
    headers: dict[str, str]

class Response:
    status_code: int
    ok: bool
    text: str
    headers: dict[str, str]
    def json(self) -> Any: ...
    def close(self) -> None: ...

class Session:
    auth: auth.AuthBase | None
    headers: dict[str, str]
    def mount(self, prefix: str, adapter: adapters.HTTPAdapter) -> None: ...
    def close(self) -> None: ...
    def request(
        self,
        method: str,
        url: str,
        *,
        data: str | bytes | None = ...,
        params: dict[str, str | int] | None = ...,
        timeout: float | None = ...,
        allow_redirects: bool = ...,
    ) -> Response: ...
    def post(
        self,
        url: str,
        *,
        data: str | bytes,
        headers: dict[str, str],
        timeout: float,
        stream: bool = ...,
        allow_redirects: bool = ...,
    ) -> Response: ...
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int | None = ...,
    ) -> Response: ...
