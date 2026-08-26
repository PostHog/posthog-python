from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import warnings
import weakref
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union
from uuid import UUID, uuid4

from typing_extensions import Unpack

from ._async_consumer import _STOP, _AsyncConsumer
from ._async_request import _build_client, _require_httpx
from .args import ID_TYPES, ExceptionArg, OptionalCaptureArgs, OptionalSetArgs
from .capture_compression import (
    CaptureCompression,
    _resolve_capture_compression,
)
from .capture_mode import CaptureMode, _resolve_capture_mode
from .client import (
    Client as _SyncClient,
    add_context_tags as _add_context_tags,
    get_identity_state as _get_identity_state,
    stringify_id as _stringify_id,
)
from .contexts import get_context_session_id as _get_context_session_id
from .exception_utils import (
    DEFAULT_CODE_VARIABLES_DETECT_SECRETS,
    DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_URL_CREDENTIALS,
    _get_current_otel_span_properties,
    exc_info_from_error,
    exception_is_already_captured,
    exceptions_from_error_tuple,
    handle_in_app,
    mark_exception_as_captured,
    try_attach_code_variables_to_frames,
)
from .request import determine_server_host, normalize_host
from .utils import _normalize_timestamp, clean, system_context
from .version import VERSION

__all__ = ["AsyncClient", "AsyncPosthog"]


class AsyncClient:
    """Asyncio-native PostHog capture client.

    ``capture()`` is a synchronous, non-blocking queue write. Use
    ``await capture_immediate()`` when the caller must wait for delivery.
    ``flush()``, ``join()``, and ``shutdown()`` are awaitable lifecycle methods.
    """

    log = logging.getLogger("posthog")

    def __init__(
        self,
        project_api_key: str,
        host: Optional[str] = None,
        debug: bool = False,
        max_queue_size: int = 10000,
        send: bool = True,
        on_error=None,
        flush_at: int = 100,
        flush_interval: float = 5.0,
        gzip: bool = False,
        max_retries: int = 3,
        timeout: int = 15,
        thread: int = 1,
        disabled: bool = False,
        disable_geoip: bool = True,
        is_server: bool = True,
        historical_migration: bool = False,
        super_properties: Optional[dict[str, Any]] = None,
        before_send=None,
        log_captured_exceptions: bool = False,
        project_root: Optional[str] = None,
        capture_exception_code_variables: bool = False,
        code_variables_mask_patterns=None,
        code_variables_ignore_patterns=None,
        code_variables_mask_url_credentials=None,
        code_variables_detect_secrets=None,
        in_app_modules: Optional[list[str]] = None,
        capture_mode: Optional[Union[CaptureMode, str]] = None,
        capture_compression: Optional[Union[CaptureCompression, str]] = None,
        capture_trace_context: bool = False,
    ) -> None:
        if flush_at <= 0:
            raise ValueError("flush_at must be greater than zero")
        if flush_interval <= 0:
            raise ValueError("flush_interval must be greater than zero")

        self.api_key = (project_api_key or "").strip()
        self.raw_host = normalize_host(host)
        self.host = determine_server_host(host)
        self.debug = debug
        self.send = send
        self.on_error = on_error
        self.gzip = gzip
        self.max_retries = max(0, max_retries)
        self.timeout = timeout
        self.disabled = disabled or not self.api_key
        self.disable_geoip = disable_geoip
        self.is_server = is_server
        self.historical_migration = historical_migration
        self.super_properties = super_properties
        self.capture_mode = _resolve_capture_mode(capture_mode)
        self.capture_compression = _resolve_capture_compression(
            capture_compression, gzip_fallback=gzip
        )
        self.capture_trace_context = capture_trace_context
        self.log_captured_exceptions = log_captured_exceptions
        self.capture_exception_code_variables = capture_exception_code_variables
        self.code_variables_mask_patterns = (
            code_variables_mask_patterns
            if code_variables_mask_patterns is not None
            else DEFAULT_CODE_VARIABLES_MASK_PATTERNS
        )
        self.code_variables_ignore_patterns = (
            code_variables_ignore_patterns
            if code_variables_ignore_patterns is not None
            else DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS
        )
        self.code_variables_mask_url_credentials = (
            code_variables_mask_url_credentials
            if code_variables_mask_url_credentials is not None
            else DEFAULT_CODE_VARIABLES_MASK_URL_CREDENTIALS
        )
        self.code_variables_detect_secrets = (
            code_variables_detect_secrets
            if code_variables_detect_secrets is not None
            else DEFAULT_CODE_VARIABLES_DETECT_SECRETS
        )
        self.in_app_modules = in_app_modules
        self.project_root = project_root
        if self.project_root is None:
            try:
                self.project_root = os.getcwd()
            except Exception:
                self.project_root = None

        if before_send is not None and not callable(before_send):
            self.log.warning("before_send is not callable, it will be ignored")
            before_send = None
        self.before_send = before_send

        if debug:
            logging.basicConfig()
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.WARNING)

        if not self.api_key:
            self.log.error(
                "api_key is empty after trimming whitespace; check your project API key"
            )

        self._queue: asyncio.Queue[Any] = asyncio.Queue(max_queue_size)
        self._worker_count = max(1, thread)
        self._flush_at = flush_at
        self._flush_interval = flush_interval
        self._consumers: list[_AsyncConsumer] = []
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._immediate_callers: dict[asyncio.Task[Any], int] = {}
        self._immediate_completions: set[asyncio.Future[None]] = set()
        self._http_client: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._accepting = True
        self._closed = False
        self._shutdown_lock = asyncio.Lock()
        self._deferred_lifecycle_tasks: set[asyncio.Task[Any]] = set()
        self._duplicate_client_registry_key: Optional[tuple[str, str]] = None
        self._register_duplicate_client()

    async def __aenter__(self) -> AsyncClient:
        self._ensure_workers_started()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.shutdown()
        return False

    def _register_duplicate_client(self) -> None:
        if self.disabled or not self.send or not self.api_key:
            return

        registry_key = (self.api_key, self.host)
        should_warn = False
        with _SyncClient._client_registry_lock:
            clients = _SyncClient._client_registry.setdefault(
                registry_key, weakref.WeakSet()
            )
            has_existing_client = len(clients) > 0
            clients.add(self)
            self._duplicate_client_registry_key = registry_key
            if (
                has_existing_client
                and registry_key not in _SyncClient._duplicate_client_warnings
            ):
                _SyncClient._duplicate_client_warnings.add(registry_key)
                should_warn = True

        if should_warn:
            self.log.warning(
                "Multiple active PostHog clients detected for the same project API key "
                "and host. Reuse one client per application when possible."
            )

    def _unregister_duplicate_client(self) -> None:
        registry_key = self._duplicate_client_registry_key
        if registry_key is None:
            return
        with _SyncClient._client_registry_lock:
            clients = _SyncClient._client_registry.get(registry_key)
            if clients is not None:
                clients.discard(self)
                if not clients:
                    del _SyncClient._client_registry[registry_key]
                    _SyncClient._duplicate_client_warnings.discard(registry_key)
        self._duplicate_client_registry_key = None

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("AsyncClient cannot be shared across event loops")
        return loop

    def _get_http_client(self):
        self._bind_loop()
        if self._http_client is None:
            self._http_client = _build_client(self.host)
        return self._http_client

    def _new_consumer(self) -> _AsyncConsumer:
        http_client = (
            self._get_http_client() if self.capture_mode == CaptureMode.V0 else None
        )
        return _AsyncConsumer(
            self._queue,
            self.api_key,
            host=self.host,
            on_error=self.on_error,
            process_event=self._process_event,
            flush_at=self._flush_at,
            flush_interval=self._flush_interval,
            gzip=self.gzip,
            retries=self.max_retries,
            timeout=self.timeout,
            historical_migration=self.historical_migration,
            capture_mode=self.capture_mode,
            capture_compression=self.capture_compression,
            http_client=http_client,
        )

    def _validate_transport_available(self) -> None:
        if self.capture_mode == CaptureMode.V0:
            _require_httpx()

    def _ensure_workers_started(self) -> None:
        if self.disabled or not self.send or self._closed or self._worker_tasks:
            return
        self._bind_loop()
        for _ in range(self._worker_count):
            consumer = self._new_consumer()
            self._consumers.append(consumer)
            self._worker_tasks.append(asyncio.create_task(consumer.run()))

    def _normalize_uuid(self, msg: dict[str, Any]) -> str:
        raw_uuid = msg.pop("uuid", None)
        if raw_uuid is not None:
            try:
                normalized = str(UUID(str(raw_uuid)))
            except (TypeError, ValueError, AttributeError):
                self.log.error(
                    "Invalid UUID %r. Falling back to a generated UUID.", raw_uuid
                )
            else:
                msg["uuid"] = normalized
                return normalized

        normalized = str(uuid4())
        msg["uuid"] = normalized
        return normalized

    def _prepare_event(
        self,
        msg: dict[str, Any],
        disable_geoip: Optional[bool],
        property_allowlist=None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if self.disabled or not self._accepting:
            return None, None

        timestamp = msg.get("timestamp")
        if timestamp is None:
            timestamp = datetime.now(tz=timezone.utc)
        try:
            msg["timestamp"] = _normalize_timestamp(timestamp)
        except ValueError:
            self.log.warning(
                "Invalid timestamp %r. Falling back to the current UTC time.",
                timestamp,
            )
            msg["timestamp"] = datetime.now(tz=timezone.utc).isoformat()

        sent_uuid = self._normalize_uuid(msg)
        properties = msg.setdefault("properties", {})
        properties["$lib"] = "posthog-python"
        properties["$lib_version"] = VERSION

        if disable_geoip is None:
            disable_geoip = self.disable_geoip
        if disable_geoip:
            properties["$geoip_disable"] = True
        if self.super_properties:
            msg["properties"] = {**properties, **self.super_properties}
        if self.is_server:
            msg["properties"]["$is_server"] = True
        if property_allowlist is not None:
            msg["properties"] = {
                key: value
                for key, value in msg["properties"].items()
                if key in property_allowlist
            }

        msg["distinct_id"] = _stringify_id(msg.get("distinct_id"))
        cleaned = clean(msg)
        return cleaned, sent_uuid

    async def _process_event(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        if self.before_send is None:
            return msg

        original_uuid = msg["uuid"]
        try:
            result = self.before_send(msg)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                self.log.debug("event dropped by before_send callback")
                return None
            if not isinstance(result, dict):
                raise TypeError("before_send must return a dict or None")
            processed = clean(result)
            processed["uuid"] = original_uuid
            return processed
        except Exception as error:
            self.log.error("Error in before_send callback (%s)", type(error).__name__)
            return None

    def _build_capture_event(
        self, event: str, kwargs: OptionalCaptureArgs
    ) -> tuple[dict[str, Any], Optional[bool], Any]:
        properties = {**(kwargs.get("properties") or {}), **system_context()}
        if self.capture_trace_context:
            properties = {**_get_current_otel_span_properties(), **properties}
        properties = _add_context_tags(properties)
        assert properties is not None

        distinct_id, personless = _get_identity_state(kwargs.get("distinct_id"))
        if personless and "$process_person_profile" not in properties:
            properties["$process_person_profile"] = False
        groups = kwargs.get("groups")
        if groups:
            properties["$groups"] = groups

        flags_snapshot = kwargs.get("flags")
        send_feature_flags = kwargs.get("send_feature_flags")
        if flags_snapshot is not None:
            properties = {**flags_snapshot._get_event_properties(), **properties}
        elif send_feature_flags:
            warnings.warn(
                "AsyncClient does not support deprecated send_feature_flags. Pass a "
                "flags snapshot from evaluate_flags() instead.",
                DeprecationWarning,
                stacklevel=3,
            )

        return (
            {
                "properties": properties,
                "timestamp": kwargs.get("timestamp"),
                "distinct_id": distinct_id,
                "event": event,
                "uuid": kwargs.get("uuid"),
            },
            kwargs.get("disable_geoip"),
            kwargs.get("_property_allowlist"),
        )

    def capture(
        self, event: str, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        """Queue an event without blocking for network delivery."""
        try:
            msg, disable_geoip, property_allowlist = self._build_capture_event(
                event, kwargs
            )
            prepared, sent_uuid = self._prepare_event(
                msg, disable_geoip, property_allowlist
            )
            if prepared is None or sent_uuid is None:
                return None
            if not self.send:
                return sent_uuid

            self._validate_transport_available()
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Capture before the loop starts is supported. flush()/shutdown()
                # will bind the client and start the workers.
                pass
            else:
                self._ensure_workers_started()

            self._queue.put_nowait(prepared)
            self.log.debug("queued async event %s", event)
            return sent_uuid
        except asyncio.QueueFull:
            self.log.warning("PostHog async capture queue is full")
            return None
        except Exception as error:
            if self.debug:
                raise
            self.log.exception("Error in async capture: %s", error)
            return None

    async def capture_immediate(
        self, event: str, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        """Capture one event and wait until its delivery attempt completes."""
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - async functions always have a task
            return None
        if not self._accepting:
            return None
        loop = self._bind_loop()
        completion: asyncio.Future[None] = loop.create_future()
        self._immediate_completions.add(completion)
        self._immediate_callers[current] = self._immediate_callers.get(current, 0) + 1
        error_batch: list[dict[str, Any]] = []
        try:
            msg, disable_geoip, property_allowlist = self._build_capture_event(
                event, kwargs
            )
            prepared, sent_uuid = self._prepare_event(
                msg, disable_geoip, property_allowlist
            )
            if prepared is None or sent_uuid is None:
                return None
            processed = await self._process_event(prepared)
            if processed is None:
                return None
            error_batch = [processed]
            if not self.send:
                return sent_uuid

            consumer = self._new_consumer()
            await consumer.request(error_batch)
            return sent_uuid
        except Exception as error:
            if self.on_error:
                try:
                    callback_result = self.on_error(error, error_batch)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                except Exception as callback_error:
                    self.log.error(
                        "on_error handler failed (%s)", type(callback_error).__name__
                    )
            if self.debug:
                raise
            self.log.error(
                "Immediate async capture failed (%s, status=%s)",
                type(error).__name__,
                getattr(error, "status", None),
            )
            return None
        finally:
            remaining_calls = self._immediate_callers[current] - 1
            if remaining_calls:
                self._immediate_callers[current] = remaining_calls
            else:
                del self._immediate_callers[current]
            if not completion.done():
                completion.set_result(None)
            self._immediate_completions.discard(completion)

    def _build_person_properties_event(
        self, event: str, property_key: str, kwargs: OptionalSetArgs
    ) -> Optional[dict[str, Any]]:
        properties = _add_context_tags(kwargs.get("properties") or {})
        distinct_id, personless = _get_identity_state(kwargs.get("distinct_id"))
        if personless or not properties:
            return None
        return {
            "timestamp": kwargs.get("timestamp"),
            "distinct_id": distinct_id,
            property_key: properties,
            "event": event,
            "uuid": kwargs.get("uuid"),
        }

    def set(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        try:
            msg = self._build_person_properties_event("$set", "$set", kwargs)
            if msg is None:
                return None
            return self._enqueue_built_event(msg, kwargs.get("disable_geoip"))
        except Exception as error:
            if self.debug:
                raise
            self.log.exception("Error in async set: %s", error)
            return None

    def set_once(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        try:
            msg = self._build_person_properties_event("$set_once", "$set_once", kwargs)
            if msg is None:
                return None
            return self._enqueue_built_event(msg, kwargs.get("disable_geoip"))
        except Exception as error:
            if self.debug:
                raise
            self.log.exception("Error in async set_once: %s", error)
            return None

    def group_identify(
        self,
        group_type: str,
        group_key: str,
        properties: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Union[datetime, str]] = None,
        uuid: Optional[Union[str, UUID]] = None,
        disable_geoip: Optional[bool] = None,
        distinct_id: Optional[ID_TYPES] = None,
    ) -> Optional[str]:
        try:
            if not _stringify_id(group_type):
                self.log.warning(
                    "group_identify() called without a group_type, dropping event"
                )
                return None
            if not _stringify_id(group_key):
                self.log.warning(
                    "group_identify() called without a group_key, dropping event"
                )
                return None

            resolved_distinct_id = _get_identity_state(distinct_id)[0]
            msg: dict[str, Any] = {
                "event": "$groupidentify",
                "properties": {
                    "$group_type": group_type,
                    "$group_key": group_key,
                    "$group_set": properties or {},
                },
                "distinct_id": resolved_distinct_id,
                "timestamp": timestamp,
                "uuid": uuid,
            }
            session_id = _get_context_session_id()
            if session_id:
                msg["properties"]["$session_id"] = str(session_id)
            return self._enqueue_built_event(msg, disable_geoip)
        except Exception as error:
            if self.debug:
                raise
            self.log.exception("Error in async group_identify: %s", error)
            return None

    def alias(
        self,
        previous_id: ID_TYPES,
        distinct_id: Optional[str],
        timestamp: Optional[Union[datetime, str]] = None,
        uuid: Optional[str] = None,
        disable_geoip: Optional[bool] = None,
    ) -> Optional[str]:
        try:
            resolved_previous_id = _stringify_id(previous_id)
            if not resolved_previous_id:
                self.log.warning("alias() called without a previous_id, dropping event")
                return None
            resolved_distinct_id, personless = _get_identity_state(distinct_id)
            if personless:
                self.log.warning("alias() called without a distinct_id, dropping event")
                return None
            msg: dict[str, Any] = {
                "properties": {
                    "distinct_id": resolved_previous_id,
                    "alias": resolved_distinct_id,
                },
                "timestamp": timestamp,
                "event": "$create_alias",
                "distinct_id": resolved_previous_id,
                "uuid": uuid,
            }
            session_id = _get_context_session_id()
            if session_id:
                msg["properties"]["$session_id"] = str(session_id)
            return self._enqueue_built_event(msg, disable_geoip)
        except Exception as error:
            if self.debug:
                raise
            self.log.exception("Error in async alias: %s", error)
            return None

    def _enqueue_built_event(
        self, msg: dict[str, Any], disable_geoip: Optional[bool]
    ) -> Optional[str]:
        prepared, sent_uuid = self._prepare_event(msg, disable_geoip)
        if prepared is None or sent_uuid is None:
            return None
        if not self.send:
            return sent_uuid
        self._validate_transport_available()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self._ensure_workers_started()
        self._queue.put_nowait(prepared)
        return sent_uuid

    def capture_exception(
        self,
        exception: Optional[ExceptionArg] = None,
        **kwargs: Unpack[OptionalCaptureArgs],
    ) -> Optional[str]:
        """Capture an exception. This method never raises, including in debug mode."""
        try:
            if exception is not None and exception_is_already_captured(exception):
                self.log.debug("Exception already captured, skipping")
                return None
            exc_info = (
                exc_info_from_error(exception)
                if exception is not None
                else sys.exc_info()
            )
            if exc_info is None or exc_info == (None, None, None):
                self.log.warning("No exception information available")
                return None

            exceptions = exceptions_from_error_tuple(exc_info)
            event = handle_in_app(
                {"exception": {"values": exceptions}},
                in_app_include=self.in_app_modules,
                project_root=self.project_root,
            )
            exceptions = event["exception"]["values"]
            properties = {
                "$exception_list": exceptions,
                **(kwargs.get("properties") or {}),
            }
            if self.capture_exception_code_variables:
                try_attach_code_variables_to_frames(
                    exceptions,
                    exc_info,
                    mask_patterns=self.code_variables_mask_patterns,
                    ignore_patterns=self.code_variables_ignore_patterns,
                    mask_url_credentials=self.code_variables_mask_url_credentials,
                    detect_secrets=self.code_variables_detect_secrets,
                )
            if self.log_captured_exceptions:
                self.log.exception(exception, extra=kwargs)

            result = self.capture(
                "$exception",
                distinct_id=kwargs.get("distinct_id"),
                properties=properties,
                timestamp=kwargs.get("timestamp"),
                uuid=kwargs.get("uuid"),
                groups=kwargs.get("groups"),
                flags=kwargs.get("flags"),
                disable_geoip=kwargs.get("disable_geoip"),
            )
            if exception is not None and result is not None:
                mark_exception_as_captured(exception, result)
            return result
        except Exception as error:
            self.log.exception("Failed to capture exception: %s", error)
            return None

    def _pending_queue_items(self) -> int:
        return int(getattr(self._queue, "_unfinished_tasks", self._queue.qsize()))

    def _defer_lifecycle_call(self, awaitable) -> None:
        task = asyncio.create_task(awaitable)
        self._deferred_lifecycle_tasks.add(task)
        task.add_done_callback(self._deferred_lifecycle_tasks.discard)

    async def flush(self, timeout_seconds: Optional[float] = 10) -> None:
        if asyncio.current_task() in self._worker_tasks:
            self._defer_lifecycle_call(self.flush(timeout_seconds))
            return
        if not self.send or self.disabled or self._pending_queue_items() == 0:
            return
        self._ensure_workers_started()
        deadline = (
            None
            if timeout_seconds is None
            else asyncio.get_running_loop().time() + timeout_seconds
        )
        try:
            for consumer in self._consumers:
                consumer.request_flush()

            if deadline is None:
                await self._queue.join()
            else:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                await asyncio.wait_for(self._queue.join(), remaining)
        except asyncio.TimeoutError:
            self.log.warning(
                "flush timed out after %s seconds with %s items pending",
                timeout_seconds,
                self._pending_queue_items(),
            )

    async def _close_transport(self) -> None:
        http_client = self._http_client
        self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    async def shutdown(self) -> None:
        current = asyncio.current_task()
        if current in self._worker_tasks or current in self._immediate_callers:
            self._accepting = False
            self._defer_lifecycle_call(self.shutdown())
            return

        async with self._shutdown_lock:
            if self._closed:
                return
            self._bind_loop()
            self._accepting = False
            errors: list[Exception] = []

            pending_immediate = [
                completion
                for completion in self._immediate_completions
                if not completion.done()
            ]
            if pending_immediate:
                await asyncio.gather(*pending_immediate, return_exceptions=True)

            try:
                await self.flush(timeout_seconds=None)
            except Exception as error:
                self.log.exception("Failed to flush async capture queue")
                errors.append(error)

            try:
                for _ in self._worker_tasks:
                    await self._queue.put(_STOP)
                if self._worker_tasks:
                    await asyncio.gather(*self._worker_tasks, return_exceptions=False)
            except Exception as error:
                self.log.exception("Failed to stop async capture workers")
                errors.append(error)
            finally:
                self._worker_tasks.clear()
                self._consumers.clear()

            try:
                await self._close_transport()
            except Exception as error:
                self.log.exception("Failed to close async capture transport")
                errors.append(error)

            try:
                self._unregister_duplicate_client()
            except Exception as error:
                self.log.exception("Failed to unregister async client")
                errors.append(error)

            self._closed = True
            if errors and self.debug:
                raise errors[0]

    async def join(self) -> None:
        await self.shutdown()


class AsyncPosthog(AsyncClient):
    """Customer-facing name for :class:`AsyncClient`."""

    pass
