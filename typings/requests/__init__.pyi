from types import TracebackType
from typing import Any

from . import adapters as adapters, exceptions as exceptions

class Response:
    status_code: int
    ok: bool
    text: str
    headers: dict[str, str]
    def json(self) -> Any: ...
    def close(self) -> None: ...
    def raise_for_status(self) -> None: ...
    def __enter__(self) -> Response: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

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
        stream: bool = ...,
    ) -> Response: ...
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = ...,
        timeout: float | None = ...,
    ) -> Response: ...
