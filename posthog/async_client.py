import asyncio
import inspect
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union
from uuid import UUID, uuid4

from typing_extensions import Unpack

from .args import ExceptionArg, OptionalCaptureArgs, OptionalSetArgs
from .async_consumer import AsyncConsumer
from .async_request import async_batch_post
from .client import (
    Client,
    _stringify_event_uuid,
    add_context_tags,
    get_identity_state,
    stringify_id,
)
from .contexts import (
    get_capture_exception_code_variables_context,
    get_code_variables_detect_secrets_context,
    get_code_variables_ignore_patterns_context,
    get_code_variables_mask_patterns_context,
    get_code_variables_mask_url_credentials_context,
    get_context_session_id,
)
from .exception_utils import (
    exc_info_from_error,
    exception_is_already_captured,
    exceptions_from_error_tuple,
    handle_in_app,
    mark_exception_as_captured,
    try_attach_code_variables_to_frames,
)
from .request import AI_EVENTS_ENDPOINT, EVENTS_ENDPOINT, is_ai_event
from .utils import clean, guess_timezone, system_context
from .version import VERSION


class AsyncClient(Client):
    """Asyncio-native PostHog client.

    This client mirrors the synchronous ``Client`` capture lifecycle but uses an
    ``asyncio.Queue`` and worker tasks instead of daemon threads.
    """

    log = logging.getLogger("posthog")

    def __init__(
        self,
        project_api_key: str,
        host=None,
        debug=False,
        max_queue_size=10000,
        send=True,
        on_error=None,
        flush_at=100,
        flush_interval=5.0,
        gzip=False,
        max_retries=3,
        sync_mode=False,
        timeout=15,
        thread=1,
        poll_interval=30,
        personal_api_key=None,
        disabled=False,
        disable_geoip=True,
        is_server=True,
        historical_migration=False,
        feature_flags_request_timeout_seconds=3,
        super_properties=None,
        enable_exception_autocapture=False,
        log_captured_exceptions=False,
        project_root=None,
        privacy_mode=False,
        before_send=None,
        flag_fallback_cache_url=None,
        enable_local_evaluation=True,
        flag_definition_cache_provider=None,
        capture_exception_code_variables=False,
        code_variables_mask_patterns=None,
        code_variables_ignore_patterns=None,
        code_variables_mask_url_credentials=None,
        code_variables_detect_secrets=None,
        in_app_modules: list[str] | None = None,
        enable_exception_autocapture_rate_limiting=False,
        exception_autocapture_bucket_size=None,
        exception_autocapture_refill_rate=None,
        exception_autocapture_refill_interval_seconds=None,
        _dedicated_ai_endpoint=False,
    ):
        # Initialize the shared Client state without starting thread consumers.
        from .exception_capture import ExceptionCapture

        super().__init__(
            project_api_key,
            host=host,
            debug=debug,
            max_queue_size=max_queue_size,
            send=False,
            on_error=on_error,
            flush_at=flush_at,
            flush_interval=flush_interval,
            gzip=gzip,
            max_retries=max_retries,
            sync_mode=True,
            timeout=timeout,
            thread=thread,
            poll_interval=poll_interval,
            personal_api_key=personal_api_key,
            disabled=disabled,
            disable_geoip=disable_geoip,
            is_server=is_server,
            historical_migration=historical_migration,
            feature_flags_request_timeout_seconds=feature_flags_request_timeout_seconds,
            super_properties=super_properties,
            enable_exception_autocapture=enable_exception_autocapture,
            log_captured_exceptions=log_captured_exceptions,
            project_root=project_root,
            privacy_mode=privacy_mode,
            before_send=before_send,
            flag_fallback_cache_url=flag_fallback_cache_url,
            enable_local_evaluation=enable_local_evaluation,
            flag_definition_cache_provider=flag_definition_cache_provider,
            capture_exception_code_variables=capture_exception_code_variables,
            code_variables_mask_patterns=code_variables_mask_patterns,
            code_variables_ignore_patterns=code_variables_ignore_patterns,
            code_variables_mask_url_credentials=code_variables_mask_url_credentials,
            code_variables_detect_secrets=code_variables_detect_secrets,
            in_app_modules=in_app_modules,
            enable_exception_autocapture_rate_limiting=enable_exception_autocapture_rate_limiting,
            exception_autocapture_bucket_size=(
                exception_autocapture_bucket_size
                if exception_autocapture_bucket_size is not None
                else ExceptionCapture.DEFAULT_BUCKET_SIZE
            ),
            exception_autocapture_refill_rate=(
                exception_autocapture_refill_rate
                if exception_autocapture_refill_rate is not None
                else ExceptionCapture.DEFAULT_REFILL_RATE
            ),
            exception_autocapture_refill_interval_seconds=(
                exception_autocapture_refill_interval_seconds
                if exception_autocapture_refill_interval_seconds is not None
                else ExceptionCapture.DEFAULT_REFILL_INTERVAL_SECONDS
            ),
            _dedicated_ai_endpoint=_dedicated_ai_endpoint,
        )
        self.send = send
        self.sync_mode = sync_mode
        self.queue: asyncio.Queue = asyncio.Queue(max_queue_size)  # type: ignore[assignment]
        self.async_consumers: list[AsyncConsumer] = []
        self._worker_tasks: list[asyncio.Task] = []
        self._thread_count = thread
        self._flush_at = flush_at
        self._flush_interval = flush_interval
        self._max_retries = max_retries
        self._closed = False

    async def __aenter__(self):
        self._ensure_workers_started()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.shutdown()
        return False

    def _ensure_workers_started(self) -> None:
        if self.disabled or not self.send or self.sync_mode or self._worker_tasks:
            return

        for _ in range(self._thread_count):
            consumer = AsyncConsumer(
                self.queue,
                self.api_key,
                host=self.host,
                on_error=self.on_error,
                flush_at=self._flush_at,
                flush_interval=self._flush_interval,
                gzip=self.gzip,
                retries=self._max_retries,
                timeout=self.timeout,
                historical_migration=self.historical_migration,
                dedicated_ai_endpoint=self._dedicated_ai_endpoint,
            )
            self.async_consumers.append(consumer)
            self._worker_tasks.append(asyncio.create_task(consumer.run()))

    async def capture(
        self, event: str, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        try:
            distinct_id = kwargs.get("distinct_id", None)
            properties = kwargs.get("properties", None)
            timestamp = kwargs.get("timestamp", None)
            uuid = kwargs.get("uuid", None)
            groups = kwargs.get("groups", None)
            flags_snapshot = kwargs.get("flags", None)
            send_feature_flags = kwargs.get("send_feature_flags", False)
            disable_geoip = kwargs.get("disable_geoip", None)

            properties = {**(properties or {}), **system_context()}
            properties = add_context_tags(properties)
            assert properties is not None

            distinct_id, personless = get_identity_state(distinct_id)
            if personless and "$process_person_profile" not in properties:
                properties["$process_person_profile"] = False

            msg = {
                "properties": properties,
                "timestamp": timestamp,
                "distinct_id": distinct_id,
                "event": event,
                "uuid": uuid,
            }

            if groups:
                properties["$groups"] = groups

            extra_properties: dict[str, Any] = {}
            if flags_snapshot is not None:
                if send_feature_flags:
                    self.log.warning(
                        "[FEATURE FLAGS] Both `flags` and `send_feature_flags` were passed to "
                        "capture(); using `flags` and ignoring `send_feature_flags`."
                    )
                extra_properties = flags_snapshot._get_event_properties()
            elif send_feature_flags:
                self.log.warning(
                    "AsyncClient.capture(send_feature_flags=...) will use the synchronous "
                    "feature flag path until async feature flags are enabled. Prefer passing "
                    "flags=await client.evaluate_flags(...)."
                )
                return await self._capture_with_sync_feature_flags(
                    msg,
                    distinct_id,
                    groups,
                    send_feature_flags,
                    disable_geoip,
                )

            if extra_properties:
                properties = {**extra_properties, **properties}
                msg["properties"] = properties

            return await self._enqueue(msg, disable_geoip)
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in capture: {e}")
            return None

    async def _capture_with_sync_feature_flags(
        self,
        msg: dict[str, Any],
        distinct_id: str,
        groups,
        send_feature_flags,
        disable_geoip,
    ) -> Optional[str]:
        flag_options = self._parse_send_feature_flags(send_feature_flags)
        feature_variants: Optional[dict[str, Union[bool, str]]] = {}
        try:
            if flag_options["only_evaluate_locally"] is True:
                feature_variants = await asyncio.to_thread(
                    self.get_all_flags,
                    distinct_id,
                    groups=(groups or {}),
                    person_properties=flag_options["person_properties"],
                    group_properties=flag_options["group_properties"],
                    disable_geoip=disable_geoip,
                    only_evaluate_locally=True,
                    flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                )
            else:
                feature_variants = await asyncio.to_thread(
                    self.get_feature_variants,
                    distinct_id,
                    groups,
                    person_properties=flag_options["person_properties"],
                    group_properties=flag_options["group_properties"],
                    disable_geoip=disable_geoip,
                    flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                )
        except Exception as e:
            self.log.exception(f"[FEATURE FLAGS] Unable to get feature variants: {e}")

        properties = msg["properties"]
        for feature, variant in (feature_variants or {}).items():
            properties[f"$feature/{feature}"] = variant
        active_feature_flags = [
            key for key, value in (feature_variants or {}).items() if value is not False
        ]
        if active_feature_flags:
            properties["$active_feature_flags"] = active_feature_flags
        return await self._enqueue(msg, disable_geoip)

    async def set(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        try:
            distinct_id = kwargs.get("distinct_id", None)
            properties = kwargs.get("properties", None) or {}
            timestamp = kwargs.get("timestamp", None)
            uuid = kwargs.get("uuid", None)
            disable_geoip = kwargs.get("disable_geoip", None)

            properties = add_context_tags(properties)
            distinct_id, personless = get_identity_state(distinct_id)
            if personless or not properties:
                return None

            msg = {
                "timestamp": timestamp,
                "distinct_id": distinct_id,
                "$set": properties,
                "event": "$set",
                "uuid": uuid,
            }
            return await self._enqueue(msg, disable_geoip)
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in set: {e}")
            return None

    async def set_once(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        try:
            distinct_id = kwargs.get("distinct_id", None)
            properties = kwargs.get("properties", None) or {}
            timestamp = kwargs.get("timestamp", None)
            uuid = kwargs.get("uuid", None)
            disable_geoip = kwargs.get("disable_geoip", None)

            properties = add_context_tags(properties)
            distinct_id, personless = get_identity_state(distinct_id)
            if personless or not properties:
                return None

            msg = {
                "timestamp": timestamp,
                "distinct_id": distinct_id,
                "$set_once": properties,
                "event": "$set_once",
                "uuid": uuid,
            }
            return await self._enqueue(msg, disable_geoip)
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in set_once: {e}")
            return None

    async def group_identify(
        self,
        group_type: str,
        group_key: str,
        properties: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Union[datetime, str]] = None,
        uuid: Optional[Union[str, UUID]] = None,
        disable_geoip: Optional[bool] = None,
        distinct_id=None,
    ) -> Optional[str]:
        try:
            distinct_id = get_identity_state(distinct_id)[0]
            msg: Dict[str, Any] = {
                "event": "$groupidentify",
                "properties": {
                    "$group_type": group_type,
                    "$group_key": group_key,
                    "$group_set": properties or {},
                },
                "distinct_id": distinct_id,
                "timestamp": timestamp,
                "uuid": uuid,
            }
            if get_context_session_id():
                msg["properties"]["$session_id"] = str(get_context_session_id())
            return await self._enqueue(msg, disable_geoip)
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in group_identify: {e}")
            return None

    async def alias(
        self,
        previous_id: str,
        distinct_id: Optional[str],
        timestamp: Optional[Union[datetime, str]] = None,
        uuid: Optional[str] = None,
        disable_geoip: Optional[bool] = None,
    ) -> Optional[str]:
        try:
            distinct_id, personless = get_identity_state(distinct_id)
            if personless:
                return None
            msg: Dict[str, Any] = {
                "properties": {"distinct_id": previous_id, "alias": distinct_id},
                "timestamp": timestamp,
                "event": "$create_alias",
                "distinct_id": previous_id,
                "uuid": uuid,
            }
            if get_context_session_id():
                msg["properties"]["$session_id"] = str(get_context_session_id())
            return await self._enqueue(msg, disable_geoip)
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in alias: {e}")
            return None

    async def capture_exception(  # type: ignore[override]
        self,
        exception: Optional[ExceptionArg] = None,
        **kwargs: Unpack[OptionalCaptureArgs],
    ) -> Optional[str]:
        try:
            distinct_id = kwargs.get("distinct_id", None)
            properties = kwargs.get("properties", None) or {}
            flags_snapshot = kwargs.get("flags", None)
            send_feature_flags = kwargs.get("send_feature_flags", False)
            disable_geoip = kwargs.get("disable_geoip", None)

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

            all_exceptions_with_trace = exceptions_from_error_tuple(exc_info)
            event = handle_in_app(
                {"exception": {"values": all_exceptions_with_trace}},
                in_app_include=self.in_app_modules,
                project_root=self.project_root,
            )
            all_exceptions_with_trace_and_in_app = event["exception"]["values"]
            properties = {
                "$exception_list": all_exceptions_with_trace_and_in_app,
                **properties,
            }

            context_enabled = get_capture_exception_code_variables_context()
            context_mask = get_code_variables_mask_patterns_context()
            context_ignore = get_code_variables_ignore_patterns_context()
            context_mask_url_credentials = (
                get_code_variables_mask_url_credentials_context()
            )
            context_detect_secrets = get_code_variables_detect_secrets_context()

            enabled = (
                context_enabled
                if context_enabled is not None
                else self.capture_exception_code_variables
            )
            mask_patterns = (
                context_mask
                if context_mask is not None
                else self.code_variables_mask_patterns
            )
            ignore_patterns = (
                context_ignore
                if context_ignore is not None
                else self.code_variables_ignore_patterns
            )
            mask_url_credentials = (
                context_mask_url_credentials
                if context_mask_url_credentials is not None
                else self.code_variables_mask_url_credentials
            )
            detect_secrets = (
                context_detect_secrets
                if context_detect_secrets is not None
                else self.code_variables_detect_secrets
            )

            if enabled:
                try_attach_code_variables_to_frames(
                    all_exceptions_with_trace_and_in_app,
                    exc_info,
                    mask_patterns=mask_patterns,
                    ignore_patterns=ignore_patterns,
                    mask_url_credentials=mask_url_credentials,
                    detect_secrets=detect_secrets,
                )

            if self.log_captured_exceptions:
                self.log.exception(exception, extra=kwargs)

            res = await self.capture(
                "$exception",
                distinct_id=distinct_id,
                properties=properties,
                timestamp=kwargs.get("timestamp", None),
                uuid=kwargs.get("uuid", None),
                groups=kwargs.get("groups", None),
                flags=flags_snapshot,
                send_feature_flags=send_feature_flags,
                disable_geoip=disable_geoip,
            )
            if exception is not None and res is not None:
                mark_exception_as_captured(exception, res)
            return res
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Failed to capture exception: {e}")
            return None

    async def _enqueue(self, msg, disable_geoip) -> Optional[str]:  # type: ignore[override]
        if self.disabled:
            return None

        timestamp = msg["timestamp"]
        if timestamp is None:
            timestamp = datetime.now(tz=timezone.utc)

        timestamp = guess_timezone(timestamp)
        msg["timestamp"] = timestamp.isoformat()

        if "uuid" in msg:
            uuid = msg.pop("uuid")
            if uuid is not None:
                try:
                    msg["uuid"] = _stringify_event_uuid(uuid)
                except ValueError as e:
                    self.log.error("%s Falling back to a generated UUID.", e)

        if "uuid" not in msg:
            msg["uuid"] = stringify_id(uuid4())

        sent_uuid = msg["uuid"]

        if not msg.get("properties"):
            msg["properties"] = {}
        msg["properties"]["$lib"] = "posthog-python"
        msg["properties"]["$lib_version"] = VERSION

        if disable_geoip is None:
            disable_geoip = self.disable_geoip
        if disable_geoip:
            msg["properties"]["$geoip_disable"] = True

        if self.super_properties:
            msg["properties"] = {**msg["properties"], **self.super_properties}

        if self.is_server:
            msg["properties"]["$is_server"] = True

        msg["distinct_id"] = stringify_id(msg.get("distinct_id", None))
        msg = clean(msg)

        if self.before_send:
            try:
                modified_msg = self.before_send(msg)
                if inspect.isawaitable(modified_msg):
                    modified_msg = await modified_msg
                if modified_msg is None:
                    self.log.debug("Event dropped by before_send callback")
                    return None
                msg = modified_msg
            except Exception as e:
                self.log.exception(f"Error in before_send callback: {e}")

        self.log.debug("queueing: %s", msg)

        if not self.send:
            return sent_uuid

        if self.sync_mode:
            self.log.debug("enqueued with async blocking %s.", msg["event"])
            path = (
                AI_EVENTS_ENDPOINT
                if self._dedicated_ai_endpoint and is_ai_event(msg.get("event"))
                else EVENTS_ENDPOINT
            )
            await async_batch_post(
                self.api_key,
                self.host,
                gzip=self.gzip,
                timeout=self.timeout,
                batch=[msg],
                historical_migration=self.historical_migration,
                path=path,
            )
            return sent_uuid

        self._ensure_workers_started()
        try:
            self.queue.put_nowait(msg)
            self.log.debug("enqueued %s.", msg["event"])
            return sent_uuid
        except asyncio.QueueFull:
            self.log.warning("analytics-python async queue is full")
            return None

    async def flush(self, timeout_seconds: Optional[float] = 10) -> None:  # type: ignore[override]
        if timeout_seconds is None:
            await self.queue.join()
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.log.warning(
                "flush timed out after %s seconds with %s items pending.",
                timeout_seconds,
                self.queue.qsize(),
            )

    async def join(self) -> None:  # type: ignore[override]
        for consumer in self.async_consumers:
            consumer.pause()
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self.async_consumers.clear()

        if self.poller:
            self.poller.stop()

        self._shutdown_flag_definition_cache_provider()
        self._unregister_duplicate_client()

    async def shutdown(self) -> None:  # type: ignore[override]
        if self._closed:
            return
        await self.flush(timeout_seconds=None)
        await self.join()
        self.distinct_ids_feature_flags_reported.clear()
        if self.exception_capture:
            self.exception_capture.close()
        self._closed = True
