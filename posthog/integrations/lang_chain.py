try:
    import langchain_core  # noqa: F401
except ImportError:
    raise ModuleNotFoundError(
        "Please install LangChain to use this feature: 'pip install langchain-core'"
    )

from typing import Any, Optional
from uuid import UUID

from posthog.client import Client

try:
    # LangChain 1.0+ and modern 0.x with langchain-core
    from langchain_core.callbacks.base import BaseCallbackHandler
except (ImportError, ModuleNotFoundError):
    # Fallback for older LangChain versions
    from langchain.callbacks.base import BaseCallbackHandler


class PostHogCallback(BaseCallbackHandler):
    raise_error: bool = True

    def __init__(self, client: Optional[Client] = None) -> None:
        self.client = client

    def capture_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        from posthog import capture_exception

        properties = {
            "$langchain_run_id": str(run_id),
            "$langchain_parent_run_id": str(parent_run_id) if parent_run_id else None,
            "$langchain_tags": tags,
        }

        capture_fn = self.client.capture_exception if self.client else capture_exception
        capture_fn(error, properties=properties)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        self.capture_error(error, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        self.capture_error(error, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        self.capture_error(error, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        self.capture_error(error, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
