from __future__ import annotations

import asyncio
import inspect
import sys
import warnings
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Union
from uuid import UUID

from typing_extensions import Unpack

from .args import ExceptionArg, OptionalCaptureArgs, OptionalSetArgs
from .feature_flag_evaluations import FeatureFlagEvaluations
from .types import (
    FeatureFlag,
    FeatureFlagError,
    FeatureFlagResult,
    FlagsAndPayloads,
    FlagsResponse,
    FlagValue,
    normalize_flags_response,
    to_flags_and_payloads,
)
from ._async_consumer import _AsyncConsumer
from ._async_request import (
    async_batch_post as _async_batch_post,
    async_flags as _async_flags,
    async_get as _async_get,
    async_remote_config as _async_remote_config,
)
from .client import Client as _Client
from .contexts import (
    get_capture_exception_code_variables_context as _get_capture_exception_code_variables_context,
    get_code_variables_detect_secrets_context as _get_code_variables_detect_secrets_context,
    get_code_variables_ignore_patterns_context as _get_code_variables_ignore_patterns_context,
    get_code_variables_mask_patterns_context as _get_code_variables_mask_patterns_context,
    get_code_variables_mask_url_credentials_context as _get_code_variables_mask_url_credentials_context,
)
from .exception_utils import (
    exc_info_from_error as _exc_info_from_error,
    exception_is_already_captured as _exception_is_already_captured,
    exceptions_from_error_tuple as _exceptions_from_error_tuple,
    handle_in_app as _handle_in_app,
    mark_exception_as_captured as _mark_exception_as_captured,
    try_attach_code_variables_to_frames as _try_attach_code_variables_to_frames,
)
from .request import (
    AI_EVENTS_ENDPOINT as _AI_EVENTS_ENDPOINT,
    EVENTS_ENDPOINT as _EVENTS_ENDPOINT,
    APIError as _APIError,
    QuotaLimitError as _QuotaLimitError,
    is_ai_event as _is_ai_event,
)
from .feature_flag_evaluations import (
    _FeatureFlagEvaluationsHost,
    _feature_flag_called_properties,
    _flag_details_metadata,
    _local_evaluation_records,
    _remote_evaluation_records,
)

__all__ = ["AsyncClient"]


class AsyncClient(_Client):
    """Asyncio-native PostHog client.

    This client mirrors the synchronous ``Client`` capture lifecycle but uses an
    ``asyncio.Queue`` and worker tasks instead of daemon threads.
    """

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
        # Initialize the shared _Client state without starting thread consumers.
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
        self._queue: asyncio.Queue = asyncio.Queue(max_queue_size)
        self._async_consumers: list[_AsyncConsumer] = []
        self._worker_tasks: list[asyncio.Task] = []
        self._thread_count = thread
        self._flush_at = flush_at
        self._flush_interval = flush_interval
        self._max_retries = max_retries
        self._closed = False
        self._flag_poll_task: Optional[asyncio.Task] = None
        self._pending_feature_flag_capture_tasks: set[asyncio.Task] = set()

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
            consumer = _AsyncConsumer(
                self._queue,
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
            self._async_consumers.append(consumer)
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

            msg, distinct_id = self._build_capture_message(
                event, distinct_id, properties, timestamp, uuid, groups
            )

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
                    "`send_feature_flags` is deprecated. Prefer passing "
                    "flags=await client.evaluate_flags(...)."
                )
                return await self._capture_with_feature_flags(
                    msg,
                    distinct_id,
                    groups,
                    send_feature_flags,
                    disable_geoip,
                )

            self._apply_capture_properties(msg, extra_properties)

            return await self._enqueue(msg, disable_geoip)
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in capture: {e}")
            return None

    async def _capture_with_feature_flags(
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
                feature_variants = await self.get_all_flags(
                    distinct_id,
                    groups=(groups or {}),
                    person_properties=flag_options["person_properties"],
                    group_properties=flag_options["group_properties"],
                    disable_geoip=disable_geoip,
                    only_evaluate_locally=True,
                    flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                )
            else:
                feature_variants = await self.get_all_flags(
                    distinct_id,
                    groups=(groups or {}),
                    person_properties=flag_options["person_properties"],
                    group_properties=flag_options["group_properties"],
                    disable_geoip=disable_geoip,
                    only_evaluate_locally=False,
                    flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                )
        except Exception as e:
            self.log.exception(f"[FEATURE FLAGS] Unable to get feature variants: {e}")

        self._apply_capture_properties(
            msg, self._feature_flag_capture_properties(feature_variants)
        )
        return await self._enqueue(msg, disable_geoip)

    async def set(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        try:
            msg = self._build_person_properties_message(
                "$set",
                "$set",
                kwargs.get("distinct_id", None),
                kwargs.get("properties", None),
                kwargs.get("timestamp", None),
                kwargs.get("uuid", None),
            )
            if msg is None:
                return None
            return await self._enqueue(msg, kwargs.get("disable_geoip", None))
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Error in set: {e}")
            return None

    async def set_once(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        try:
            msg = self._build_person_properties_message(
                "$set_once",
                "$set_once",
                kwargs.get("distinct_id", None),
                kwargs.get("properties", None),
                kwargs.get("timestamp", None),
                kwargs.get("uuid", None),
            )
            if msg is None:
                return None
            return await self._enqueue(msg, kwargs.get("disable_geoip", None))
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
            msg = self._build_group_identify_message(
                group_type, group_key, properties, timestamp, uuid, distinct_id
            )
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
            msg = self._build_alias_message(previous_id, distinct_id, timestamp, uuid)
            if msg is None:
                return None
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

            if exception is not None and _exception_is_already_captured(exception):
                self.log.debug("Exception already captured, skipping")
                return None

            exc_info = (
                _exc_info_from_error(exception)
                if exception is not None
                else sys.exc_info()
            )
            if exc_info is None or exc_info == (None, None, None):
                self.log.warning("No exception information available")
                return None

            all_exceptions_with_trace = _exceptions_from_error_tuple(exc_info)
            event = _handle_in_app(
                {"exception": {"values": all_exceptions_with_trace}},
                in_app_include=self.in_app_modules,
                project_root=self.project_root,
            )
            all_exceptions_with_trace_and_in_app = event["exception"]["values"]
            properties = {
                "$exception_list": all_exceptions_with_trace_and_in_app,
                **properties,
            }

            context_enabled = _get_capture_exception_code_variables_context()
            context_mask = _get_code_variables_mask_patterns_context()
            context_ignore = _get_code_variables_ignore_patterns_context()
            context_mask_url_credentials = (
                _get_code_variables_mask_url_credentials_context()
            )
            context_detect_secrets = _get_code_variables_detect_secrets_context()

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
                _try_attach_code_variables_to_frames(
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
                _mark_exception_as_captured(exception, res)
            return res
        except Exception as e:
            if self.debug:
                raise
            self.log.exception(f"Failed to capture exception: {e}")
            return None

    async def _enqueue(self, msg, disable_geoip) -> Optional[str]:  # type: ignore[override]
        msg, sent_uuid = self._prepare_enqueue_message(msg, disable_geoip)
        if msg is None or sent_uuid is None:
            return None

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
                _AI_EVENTS_ENDPOINT
                if self._dedicated_ai_endpoint and _is_ai_event(msg.get("event"))
                else _EVENTS_ENDPOINT
            )
            await _async_batch_post(
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
            self._queue.put_nowait(msg)
            self.log.debug("enqueued %s.", msg["event"])
            return sent_uuid
        except asyncio.QueueFull:
            self.log.warning("analytics-python async queue is full")
            return None

    async def get_flags_decision(  # type: ignore[override]
        self,
        distinct_id=None,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsResponse:
        try:
            return await self._get_flags_decision(
                distinct_id,
                groups,
                person_properties,
                group_properties,
                disable_geoip,
                flag_keys_to_evaluate,
                device_id=device_id,
            )
        except Exception as err:
            self.log.exception("Unable to get feature flags: %s", err)
            return normalize_flags_response({})

    async def _get_flags_decision(  # type: ignore[override]
        self,
        distinct_id=None,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsResponse:
        if self.disabled:
            return normalize_flags_response({})

        from .contexts import get_context_device_id, get_context_distinct_id

        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        if distinct_id is None:
            distinct_id = get_context_distinct_id()
        if device_id is None:
            device_id = get_context_device_id()
        if disable_geoip is None:
            disable_geoip = self.disable_geoip

        request_data: Dict[str, Any] = {
            "distinct_id": distinct_id,
            "groups": groups,
            "person_properties": person_properties,
            "group_properties": group_properties,
            "geoip_disable": disable_geoip,
            "device_id": device_id,
        }
        if flag_keys_to_evaluate:
            request_data["flag_keys_to_evaluate"] = flag_keys_to_evaluate

        resp_data = await _async_flags(
            self.api_key,
            self.host,
            timeout=self.feature_flags_request_timeout_seconds,
            **request_data,
        )
        return normalize_flags_response(resp_data)

    async def _resolve_flag_definition_cache_provider_result_async(self, result):
        if inspect.isawaitable(result):
            return await result
        return result

    async def _shutdown_flag_definition_cache_provider_async(self) -> None:
        if not self._flag_definition_cache_provider:
            return
        try:
            await self._resolve_flag_definition_cache_provider_result_async(
                self._flag_definition_cache_provider.shutdown()
            )
        except Exception as e:
            self.log.error(f"[FEATURE FLAGS] Cache provider shutdown error: {e}")

    async def _load_feature_flags_async(self) -> None:
        should_fetch = True
        if self._flag_definition_cache_provider:
            try:
                should_fetch = await self._resolve_flag_definition_cache_provider_result_async(
                    self._flag_definition_cache_provider.should_fetch_flag_definitions()
                )
            except Exception as e:
                self.log.error(
                    f"[FEATURE FLAGS] Cache provider should_fetch error: {e}"
                )
                should_fetch = True

        if not should_fetch and self._flag_definition_cache_provider:
            try:
                cached_data = (
                    await self._resolve_flag_definition_cache_provider_result_async(
                        self._flag_definition_cache_provider.get_flag_definitions()
                    )
                )
                if cached_data:
                    self.log.debug(
                        "[FEATURE FLAGS] Using cached flag definitions from external cache"
                    )
                    self._update_flag_state(
                        cached_data, old_flags_by_key=self.feature_flags_by_key or {}
                    )
                    self._last_feature_flag_poll = datetime.now(tz=timezone.utc)
                    return
                if not self.feature_flags:
                    self.log.debug(
                        "[FEATURE FLAGS] Cache empty and no flags loaded, falling back to API fetch"
                    )
                    should_fetch = True
            except Exception as e:
                self.log.error(f"[FEATURE FLAGS] Cache provider get error: {e}")
                should_fetch = True

        if should_fetch:
            await self._fetch_feature_flags_from_api_async()

    async def _fetch_feature_flags_from_api_async(self) -> None:
        personal_api_key = self.personal_api_key
        if personal_api_key is None:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a personal_api_key to use feature flags."
            )
            return

        try:
            old_flags_by_key: dict[str, dict] = self.feature_flags_by_key or {}
            response = await _async_get(
                personal_api_key,
                f"/flags/definitions?token={self.api_key}&send_cohorts",
                self.host,
                timeout=10,
                etag=self._flags_etag,
            )
            self._flags_etag = response.etag
            if response.not_modified:
                self.log.debug(
                    "[FEATURE FLAGS] Flags not modified (304), using cached data"
                )
                self._last_feature_flag_poll = datetime.now(tz=timezone.utc)
                return
            if response.data is None:
                self.log.error(
                    "[FEATURE FLAGS] Unexpected empty response data in non-304 response"
                )
                return

            self._update_flag_state(response.data, old_flags_by_key=old_flags_by_key)
            if self._flag_definition_cache_provider:
                try:
                    await self._resolve_flag_definition_cache_provider_result_async(
                        self._flag_definition_cache_provider.on_flag_definitions_received(
                            {
                                "flags": self.feature_flags or [],
                                "group_type_mapping": self.group_type_mapping or {},
                                "cohorts": self.cohorts or {},
                            }
                        )
                    )
                except Exception as e:
                    self.log.error(f"[FEATURE FLAGS] Cache provider store error: {e}")
        except _APIError as e:
            if e.status == 401:
                detail = (
                    f"Error loading feature flags: {e.message}. "
                    "Please verify both your project_api_key and personal_api_key. "
                    "More information: https://posthog.com/docs/api/overview"
                )
                self.log.error("[FEATURE FLAGS] %s", detail)
                self.feature_flags = []
                self.group_type_mapping = {}
                self.cohorts = {}
                if self.flag_cache:
                    self.flag_cache.clear()
                if self.debug:
                    raise _APIError(status=401, message=detail)
            elif e.status == 402:
                self.log.warning(
                    "[FEATURE FLAGS] PostHog feature flags quota limited, resetting feature flag data.  Learn more about billing limits at https://posthog.com/docs/billing/limits-alerts"
                )
                self.feature_flags = []
                self.group_type_mapping = {}
                self.cohorts = {}
                if self.flag_cache:
                    self.flag_cache.clear()
                if self.debug:
                    raise _APIError(
                        status=402, message="PostHog feature flags quota limited"
                    )
            else:
                self.log.error(f"[FEATURE FLAGS] Error loading feature flags: {e}")
        except Exception as e:
            self.log.warning(
                "[FEATURE FLAGS] Fetching feature flags failed with following error. We will retry in %s seconds."
                % self.poll_interval
            )
            self.log.warning(e)

        self._last_feature_flag_poll = datetime.now(tz=timezone.utc)

    async def load_feature_flags(self) -> None:
        if self.disabled:
            self.feature_flags = []
            return
        if not self.personal_api_key:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a personal_api_key to use feature flags."
            )
            self.feature_flags = []
            return

        await self._load_feature_flags_async()

        if self.enable_local_evaluation and self._flag_poll_task is None:
            self._flag_poll_task = asyncio.create_task(self._poll_feature_flags())

    async def _poll_feature_flags(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                await self._load_feature_flags_async()
        except asyncio.CancelledError:
            raise

    async def _ensure_feature_flags_loaded_for_local_evaluation(self) -> None:
        if self.feature_flags is None and self.personal_api_key:
            await self.load_feature_flags()

    async def _get_feature_flag_details_from_server(  # type: ignore[override]
        self,
        key: str,
        distinct_id,
        groups: Mapping[str, Union[str, int]],
        person_properties: dict[str, Any],
        group_properties: dict[str, dict[str, Any]],
        disable_geoip: Optional[bool],
        device_id: Optional[str] = None,
    ) -> tuple[Optional[FeatureFlag], Optional[str], Optional[int], bool]:
        resp_data = await self._get_flags_decision(
            distinct_id,
            groups,
            person_properties,
            group_properties,
            disable_geoip,
            flag_keys_to_evaluate=[key],
            device_id=device_id,
        )
        request_id = resp_data.get("requestId")
        evaluated_at = resp_data.get("evaluatedAt")
        errors_while_computing = resp_data.get("errorsWhileComputingFlags", False)
        flags = resp_data.get("flags")
        flag_details = flags.get(key) if flags else None
        return flag_details, request_id, evaluated_at, errors_while_computing

    async def get_feature_flag_result(  # type: ignore[override]
        self,
        key: str,
        distinct_id,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = True,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[FeatureFlagResult]:
        return await self._get_feature_flag_result(
            key,
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
            device_id=device_id,
        )

    async def _get_feature_flag_result(  # type: ignore[override]
        self,
        key: str,
        distinct_id,
        *,
        override_match_value: Optional[FlagValue] = None,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = True,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[FeatureFlagResult]:
        if self.disabled:
            return None

        await self._ensure_feature_flags_loaded_for_local_evaluation()
        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                distinct_id,
                groups or {},
                person_properties or {},
                group_properties or {},
            )
        )
        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        from .contexts import get_context_device_id

        if device_id is None:
            device_id = get_context_device_id()

        flag_result = None
        flag_details = None
        request_id = None
        evaluated_at = None
        feature_flag_error: Optional[str] = None

        flag_value = self._locally_evaluate_flag(
            key, distinct_id, groups, person_properties, group_properties, device_id
        )
        flag_was_locally_evaluated = flag_value is not None

        if flag_was_locally_evaluated:
            lookup_match_value = override_match_value or flag_value
            payload = (
                self._compute_payload_locally(key, lookup_match_value)
                if lookup_match_value is not None
                else None
            )
            flag_result = FeatureFlagResult.from_value_and_payload(
                key, lookup_match_value, payload
            )
            if self.flag_cache and flag_result:
                self.flag_cache.set_cached_flag(
                    distinct_id, key, flag_result, self.flag_definition_version
                )
        elif only_evaluate_locally:
            if self.feature_flags is None:
                self.log.warning(
                    "[FEATURE FLAGS] Local evaluation called but feature flag definitions are not loaded yet. "
                    "Returning None. You can call load_feature_flags() to load flags explicitly."
                )
        else:
            try:
                (
                    flag_details,
                    request_id,
                    evaluated_at,
                    errors_while_computing,
                ) = await self._get_feature_flag_details_from_server(
                    key,
                    distinct_id,
                    groups,
                    person_properties,
                    group_properties,
                    disable_geoip,
                    device_id=device_id,
                )
                errors = []
                if errors_while_computing:
                    errors.append(FeatureFlagError.ERRORS_WHILE_COMPUTING)
                if flag_details is None:
                    errors.append(FeatureFlagError.FLAG_MISSING)
                if errors:
                    feature_flag_error = ",".join(errors)

                flag_result = FeatureFlagResult.from_flag_details(
                    flag_details, override_match_value
                )
                if self.flag_cache and flag_result:
                    self.flag_cache.set_cached_flag(
                        distinct_id, key, flag_result, self.flag_definition_version
                    )
            except _QuotaLimitError as e:
                self.log.warning(f"[FEATURE FLAGS] Quota limit exceeded: {e}")
                feature_flag_error = FeatureFlagError.QUOTA_LIMITED
                flag_result = self._get_stale_flag_fallback(distinct_id, key)
            except _APIError as e:
                self.log.warning(f"[FEATURE FLAGS] API error: {e}")
                feature_flag_error = FeatureFlagError.api_error(e.status)
                flag_result = self._get_stale_flag_fallback(distinct_id, key)
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get flag remotely: {e}")
                feature_flag_error = FeatureFlagError.UNKNOWN_ERROR
                flag_result = self._get_stale_flag_fallback(distinct_id, key)

        if send_feature_flag_events:
            await self._capture_feature_flag_called_async(
                distinct_id,
                key,
                flag_result.get_value() if flag_result else None,
                flag_result.payload if flag_result else None,
                flag_was_locally_evaluated,
                groups,
                disable_geoip,
                request_id,
                evaluated_at,
                flag_details,
                feature_flag_error,
            )

        return flag_result

    async def get_feature_flag(  # type: ignore[override]
        self,
        key: str,
        distinct_id,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = True,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[FlagValue]:
        warnings.warn(
            "`get_feature_flag` is deprecated and will be removed in a future major version. "
            "Use `await posthog.evaluate_flags(distinct_id, ...)` and call `flags.get_flag(key)` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        result = await self._get_feature_flag_result(
            key,
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
            device_id=device_id,
        )
        return result.get_value() if result else None

    async def feature_enabled(  # type: ignore[override]
        self, key: str, distinct_id, **kwargs
    ) -> Optional[bool]:
        warnings.warn(
            "`feature_enabled` is deprecated and will be removed in a future major version. "
            "Use `await posthog.evaluate_flags(distinct_id, ...)` and call `flags.is_enabled(key)` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        result = await self._get_feature_flag_result(key, distinct_id, **kwargs)
        value = result.get_value() if result else None
        return None if value is None else bool(value)

    async def get_feature_flag_payload(
        self,
        key: str,
        distinct_id,
        *,
        match_value: Optional[FlagValue] = None,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = False,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[object]:
        warnings.warn(
            "`get_feature_flag_payload` is deprecated and will be removed in a future major version. "
            "Use `await posthog.evaluate_flags(distinct_id, ...)` and call `flags.get_flag_payload(key)` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        result = await self._get_feature_flag_result(
            key,
            distinct_id,
            override_match_value=match_value,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
            device_id=device_id,
        )
        return result.payload if result else None

    async def get_all_flags(  # type: ignore[override]
        self,
        distinct_id,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> Optional[dict[str, FlagValue]]:
        response = await self.get_all_flags_and_payloads(
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            disable_geoip=disable_geoip,
            flag_keys_to_evaluate=flag_keys_to_evaluate,
            device_id=device_id,
        )
        return response["featureFlags"]

    async def get_all_flags_and_payloads(  # type: ignore[override]
        self,
        distinct_id,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsAndPayloads:
        if self.disabled:
            return {"featureFlags": None, "featureFlagPayloads": None}

        await self._ensure_feature_flags_loaded_for_local_evaluation()
        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                distinct_id, groups, person_properties, group_properties
            )
        )

        from .contexts import get_context_device_id

        if device_id is None:
            device_id = get_context_device_id()
        groups = groups or {}

        response, fallback_to_flags = self._get_all_flags_and_payloads_locally(
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            flag_keys_to_evaluate=flag_keys_to_evaluate,
            device_id=device_id,
        )

        if fallback_to_flags and not only_evaluate_locally:
            try:
                decide_response = await self._get_flags_decision(
                    distinct_id,
                    groups=groups,
                    person_properties=person_properties,
                    group_properties=group_properties,
                    disable_geoip=disable_geoip,
                    flag_keys_to_evaluate=flag_keys_to_evaluate,
                    device_id=device_id,
                )
                return to_flags_and_payloads(decide_response)
            except Exception as e:
                self.log.exception(
                    f"[FEATURE FLAGS] Unable to get feature flags and payloads: {e}"
                )
        return response

    async def evaluate_flags(  # type: ignore[override]
        self,
        distinct_id=None,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        disable_geoip: Optional[bool] = None,
        flag_keys: Optional[List[str]] = None,
        device_id: Optional[str] = None,
    ) -> FeatureFlagEvaluations:
        from .contexts import get_context_device_id, get_context_distinct_id

        host = self._get_feature_flag_evaluations_host()
        if distinct_id is None:
            distinct_id = get_context_distinct_id()
        if device_id is None:
            device_id = get_context_device_id()
        if not distinct_id or self.disabled:
            return FeatureFlagEvaluations(host=host, distinct_id="", flags={})

        await self._ensure_feature_flags_loaded_for_local_evaluation()
        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                distinct_id,
                groups or {},
                person_properties or {},
                group_properties or {},
            )
        )
        groups = groups or {}

        request_id: Optional[str] = None
        evaluated_at: Optional[int] = None
        errors_while_computing = False
        quota_limited = False

        local_result, fallback_to_server = self._get_all_flags_and_payloads_locally(
            distinct_id,
            groups=dict(groups),
            person_properties=person_properties,
            group_properties=group_properties,
            flag_keys_to_evaluate=flag_keys,
            device_id=device_id,
        )
        records, locally_evaluated_keys = _local_evaluation_records(
            local_result, self.feature_flags_by_key or {}
        )

        if fallback_to_server and not only_evaluate_locally:
            try:
                response = await self._get_flags_decision(
                    distinct_id,
                    groups=groups,
                    person_properties=person_properties,
                    group_properties=group_properties,
                    disable_geoip=disable_geoip,
                    flag_keys_to_evaluate=flag_keys,
                    device_id=device_id,
                )
                (
                    remote_records,
                    request_id,
                    evaluated_at,
                    errors_while_computing,
                ) = _remote_evaluation_records(response, locally_evaluated_keys)
                records.update(remote_records)
            except _QuotaLimitError as e:
                self.log.warning(f"[FEATURE FLAGS] Quota limit exceeded: {e}")
                quota_limited = True
            except Exception as e:
                self.log.exception(
                    f"[FEATURE FLAGS] Unable to evaluate flags remotely: {e}"
                )

        return FeatureFlagEvaluations(
            host=host,
            distinct_id=str(distinct_id),
            flags=records,
            groups=groups,
            disable_geoip=disable_geoip,
            request_id=request_id,
            evaluated_at=evaluated_at,
            errors_while_computing=errors_while_computing,
            quota_limited=quota_limited,
        )

    async def get_remote_config_payload(self, key: str):
        if self.disabled:
            return None
        if self.personal_api_key is None:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a personal_api_key to fetch decrypted feature flag payloads."
            )
            return None
        try:
            return await _async_remote_config(
                self.personal_api_key,
                self.api_key,
                self.host,
                key,
                timeout=self.feature_flags_request_timeout_seconds,
            )
        except Exception as e:
            self.log.exception(
                f"[FEATURE FLAGS] Unable to get decrypted feature flag payload: {e}"
            )

    def _get_feature_flag_evaluations_host(self) -> _FeatureFlagEvaluationsHost:
        if self._feature_flag_evaluations_host_cache is None:
            self._feature_flag_evaluations_host_cache = _FeatureFlagEvaluationsHost(
                capture_flag_called_event_if_needed=self._schedule_feature_flag_called_event,
                log_warning=lambda message: self.log.warning(message),
            )
        return self._feature_flag_evaluations_host_cache

    def _schedule_feature_flag_called_event(self, **kwargs) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.log.warning(
                "[FEATURE FLAGS] Unable to capture $feature_flag_called because no event loop is running. "
                "Access FeatureFlagEvaluations results before the async client shuts down."
            )
            return

        task = loop.create_task(
            self._capture_feature_flag_called_if_needed_async(**kwargs)
        )
        self._pending_feature_flag_capture_tasks.add(task)
        task.add_done_callback(self._pending_feature_flag_capture_tasks.discard)

    async def _capture_feature_flag_called_async(
        self,
        distinct_id,
        key: str,
        response: Optional[FlagValue],
        payload: Optional[str],
        flag_was_locally_evaluated: bool,
        groups: Mapping[str, Union[str, int]],
        disable_geoip: Optional[bool],
        request_id: Optional[str],
        evaluated_at: Optional[int],
        flag_details: Optional[FeatureFlag],
        feature_flag_error: Optional[str] = None,
    ) -> None:
        flag_id, flag_version, flag_reason = _flag_details_metadata(flag_details)
        properties = _feature_flag_called_properties(
            key=key,
            response=response,
            locally_evaluated=flag_was_locally_evaluated,
            payload=payload,
            request_id=request_id,
            evaluated_at=evaluated_at,
            flag_id=flag_id,
            flag_version=flag_version,
            flag_reason=flag_reason,
            feature_flag_error=feature_flag_error,
        )

        await self._capture_feature_flag_called_if_needed_async(
            distinct_id=distinct_id,
            key=key,
            response=response,
            properties=properties,
            groups=groups,
            disable_geoip=disable_geoip,
        )

    async def _capture_feature_flag_called_if_needed_async(
        self,
        *,
        distinct_id,
        key: str,
        response: Optional[FlagValue],
        properties: dict[str, Any],
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        disable_geoip: Optional[bool] = None,
    ) -> None:
        groups_key = (
            tuple(sorted((str(k), str(v)) for k, v in groups.items())) if groups else ()
        )
        feature_flag_reported_key = (key, response, groups_key)
        reported_flags = self.distinct_ids_feature_flags_reported.get(distinct_id)
        if reported_flags is None:
            reported_flags = set()
            self.distinct_ids_feature_flags_reported[distinct_id] = reported_flags
        if feature_flag_reported_key in reported_flags:
            return

        await self.capture(
            "$feature_flag_called",
            distinct_id=distinct_id,
            properties=properties,
            groups={str(k): str(v) for k, v in (groups or {}).items()},
            disable_geoip=disable_geoip,
        )
        reported_flags.add(feature_flag_reported_key)

    async def _drain_pending_feature_flag_captures(self) -> None:
        while self._pending_feature_flag_capture_tasks:
            tasks = list(self._pending_feature_flag_capture_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def flush(self, timeout_seconds: Optional[float] = 10) -> None:  # type: ignore[override]
        await self._drain_pending_feature_flag_captures()
        if timeout_seconds is None:
            await self._queue.join()
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.log.warning(
                "flush timed out after %s seconds with %s items pending.",
                timeout_seconds,
                self._queue.qsize(),
            )

    async def join(self) -> None:  # type: ignore[override]
        for consumer in self._async_consumers:
            consumer.pause()
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._async_consumers.clear()

        if self._flag_poll_task is not None:
            self._flag_poll_task.cancel()
            await asyncio.gather(self._flag_poll_task, return_exceptions=True)
            self._flag_poll_task = None

        await asyncio.to_thread(self._stop_blocking_polling_resources)
        await self._shutdown_flag_definition_cache_provider_async()
        await asyncio.to_thread(self._unregister_duplicate_client)

    def _stop_blocking_polling_resources(self) -> None:
        if self.poller:
            self.poller.stop()

    async def shutdown(self) -> None:  # type: ignore[override]
        if self._closed:
            return
        await self.flush(timeout_seconds=None)
        await self.join()
        self.distinct_ids_feature_flags_reported.clear()
        if self.exception_capture:
            self.exception_capture.close()
        self._closed = True
