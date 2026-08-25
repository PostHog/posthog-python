import atexit
import inspect
import json
import logging
import os
import sys
import threading
import time
import warnings
import weakref
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Union, cast
from uuid import UUID, uuid4

from typing_extensions import Unpack

from posthog._async_utils import _BackgroundEventLoopRunner
from posthog._disabled_lane_queue import _DisabledLaneQueue
from posthog.args import ID_TYPES, ExceptionArg, OptionalCaptureArgs, OptionalSetArgs
from posthog.metrics_capture import PostHogMetrics
from posthog.capture_compression import (
    CaptureCompression,
    _resolve_capture_compression,
)
from posthog.capture_mode import CaptureMode, _resolve_capture_mode
from posthog.capture_v1 import _send_v1_batch
from posthog.consumer import AI_MAX_MSG_SIZE, MAX_MSG_SIZE, Consumer, _DrainSignal
from posthog.contexts import (
    _get_current_context,
    get_capture_exception_code_variables_context,
    get_code_variables_detect_secrets_context,
    get_code_variables_ignore_patterns_context,
    get_code_variables_mask_patterns_context,
    get_code_variables_mask_url_credentials_context,
    get_context_device_id,
    get_context_distinct_id,
    get_context_session_id,
    get_tags as _context_get_tags,
    identify_context as _context_identify_context,
    _scoped as _context_scoped,
    new_context,
    set_context_device_id as _context_set_context_device_id,
    set_context_session as _context_set_context_session,
    tag as _context_tag,
)
from posthog.exception_capture import ExceptionCapture
from posthog._logging import _configure_posthog_logging
from posthog.exception_utils import (
    DEFAULT_CODE_VARIABLES_DETECT_SECRETS,
    DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_URL_CREDENTIALS,
    exc_info_from_error,
    exception_is_already_captured,
    exceptions_from_error_tuple,
    _get_current_otel_span_properties,
    handle_in_app,
    mark_exception_as_captured,
    try_attach_code_variables_to_frames,
)
from posthog.feature_flag_evaluations import (
    FeatureFlagEvaluations,
    _EvaluatedFlagRecord,
    _FeatureFlagEvaluationsHost,
)
from posthog.feature_flags import (
    InconclusiveMatchError,
    RequiresServerEvaluation,
    match_feature_flag_properties,
    resolve_bucketing_value,
)
from posthog.flag_definition_cache import (
    FlagDefinitionCacheData,
    FlagDefinitionCacheProvider,
)
from posthog.poller import Poller
from posthog.request import (
    AI_EVENTS_ENDPOINT,
    EVENTS_ENDPOINT,
    APIError,
    QuotaLimitError,
    RequestsConnectionError,
    RequestsTimeout,
    batch_post,
    determine_server_host,
    flags,
    get,
    normalize_host,
    remote_config,
    reset_sessions,
)
from posthog.types import (
    FeatureFlag,
    FeatureFlagError,
    FeatureFlagResult,
    FlagMetadata,
    FlagsAndPayloads,
    FlagsResponse,
    FlagValue,
    SendFeatureFlagsOptions,
    normalize_flags_response,
    to_flags_and_payloads,
    to_payloads,
    to_values,
)
from posthog.utils import (
    FlagCache,
    RedisFlagCache,
    SizeLimitedDict,
    clean,
    _normalize_timestamp,
    guess_timezone as guess_timezone,
    system_context,
)
from posthog.version import VERSION


from queue import Empty, Full, Queue


_configure_posthog_logging()

MAX_DICT_SIZE = 50_000
_ATEXIT_FLUSH_TIMEOUT_SECONDS = 1.0
_atexit_deadline: Optional[float] = None
_atexit_deadline_lock = threading.Lock()


def _supports_lane_synchronization(queue) -> bool:
    return all(
        hasattr(queue, attribute)
        for attribute in (
            "mutex",
            "not_empty",
            "not_full",
            "all_tasks_done",
            "unfinished_tasks",
            "_qsize",
            "_get",
        )
    )


def _new_lane_queue(maxsize: int) -> Queue:
    """Return a safe queue, disabling the lane instead of raising on failure."""
    log = logging.getLogger("posthog")
    try:
        queue: Queue = Queue(maxsize)
    except Exception:
        log.exception(
            "Failed to initialize queue.Queue; disabling asynchronous capture for the lane"
        )
        return cast(Queue, _DisabledLaneQueue(maxsize))

    if _supports_lane_synchronization(queue):
        return queue

    monkey = sys.modules.get("gevent.monkey")
    if monkey is None:
        log.error(
            "queue.Queue lacks the synchronization interface required by PostHog "
            "and gevent.monkey is not loaded; disabling asynchronous capture for the lane"
        )
        return cast(Queue, _DisabledLaneQueue(maxsize))

    try:
        if not monkey.is_object_patched("queue", "Queue"):
            log.error(
                "queue.Queue lacks the synchronization interface required by PostHog "
                "but gevent does not report it as patched; disabling asynchronous "
                "capture for the lane"
            )
            return cast(Queue, _DisabledLaneQueue(maxsize))

        original_queue = monkey.get_original("queue", "Queue")
        queue = cast(Queue, original_queue(maxsize))
    except Exception:
        log.exception(
            "Failed to restore the original queue.Queue after gevent monkey-patching; "
            "disabling asynchronous capture for the lane"
        )
        return cast(Queue, _DisabledLaneQueue(maxsize))

    if _supports_lane_synchronization(queue):
        return queue

    log.error(
        "The queue.Queue restored after gevent monkey-patching lacks the synchronization "
        "interface required by PostHog; disabling asynchronous capture for the lane"
    )
    return cast(Queue, _DisabledLaneQueue(maxsize))


def _get_atexit_deadline() -> float:
    global _atexit_deadline
    with _atexit_deadline_lock:
        if _atexit_deadline is None:
            _atexit_deadline = time.monotonic() + _ATEXIT_FLUSH_TIMEOUT_SECONDS
        return _atexit_deadline


def get_identity_state(passed) -> tuple[str, bool]:
    """Returns the distinct id to use, and whether this is a personless event or not"""
    stringified = stringify_id(passed)
    if stringified and len(stringified):
        return (stringified, False)

    context_id = get_context_distinct_id()
    if context_id:
        return (context_id, False)

    return (str(uuid4()), True)


def _stringify_event_uuid(value) -> str:
    if isinstance(value, UUID):
        return str(value)

    stringified = stringify_id(value)
    if not stringified:
        raise ValueError(
            f"Invalid event uuid {value!r}. Expected a valid UUID string or uuid.UUID instance."
        )

    try:
        UUID(stringified)
    except ValueError:
        raise ValueError(
            f"Invalid event uuid {value!r}. Expected a valid UUID string or uuid.UUID instance."
        ) from None

    return stringified


def add_context_tags(properties):
    properties = properties or {}
    current_context = _get_current_context()
    if current_context:
        context_tags = current_context.collect_tags()
        properties["$context_tags"] = set(context_tags.keys())
        # We want explicitly passed properties to override context tags
        context_tags.update(properties)
        properties = context_tags

    if "$session_id" not in properties and get_context_session_id():
        properties["$session_id"] = get_context_session_id()

    return properties


def no_throw(default_return=None):
    """
    Decorator to prevent raising exceptions from public API methods.
    Note that this doesn't prevent errors from propagating via `on_error`.
    Exceptions will still be raised if the debug flag is enabled.

    Args:
        default_return: Value to return on exception (default: None)
    """

    def decorator(func):
        from functools import wraps

        @wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                if self.debug:
                    raise e
                self.log.exception(f"Error in {func.__name__}: {e}")
                return default_return

        return wrapper

    return decorator


# Strict allowlist for minimal ``$feature_flag_called`` events, per the cross-SDK
# contract: everything else — customer-passed properties, super properties, context
# tags, and the richer parts of system context — is stripped from the
# fully-enriched properties dict. The static platform/runtime identity keys below
# are the exception: they're cheap and useful for debugging flag behavior by
# platform, so they survive minimization.
_MINIMAL_FLAG_CALLED_EVENT_PROPERTIES: frozenset[str] = frozenset(
    {
        # Identity
        "$feature_flag",
        "$feature_flag_response",
        "$feature_flag_has_experiment",
        # Evaluation debug
        "$feature_flag_id",
        "$feature_flag_version",
        "$feature_flag_reason",
        "$feature_flag_request_id",
        "$feature_flag_evaluated_at",
        "$feature_flag_error",
        "locally_evaluated",
        # Correctness-required
        "$groups",
        "$process_person_profile",
        # Linkage / SDK identity
        "$session_id",
        "$lib",
        "$lib_version",
        "$is_server",
        # Processing-control sentinel this SDK sets to deliver the event correctly
        "$geoip_disable",
        # Static platform/runtime identity: cheap, low-cardinality dimensions kept
        # for platform/runtime breakdowns on flag-call debugging.
        "$os",
        "$os_version",
        "$os_distro",
        "$python_runtime",
        "$python_version",
    }
)


def _parse_has_experiment(value: Any) -> Optional[bool]:
    """Server-reported experiment linkage; anything but an explicit bool means unknown."""
    return value if isinstance(value, bool) else None


def _parse_flag_payload(raw_payload: Any) -> Optional[Any]:
    """Flag payloads are stored as JSON strings, both in the ``/flags`` response
    metadata and in the local-evaluation flag definitions, so decode them before
    handing them to callers. A string that isn't valid JSON is passed through as-is."""
    if isinstance(raw_payload, str):
        if not raw_payload:
            return None
        try:
            return json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            return raw_payload
    return raw_payload


def _metadata_has_experiment(metadata: Any) -> Optional[bool]:
    """Server-reported experiment linkage from flag metadata; ``None`` when absent
    (e.g. ``LegacyFlagMetadata``, which doesn't carry the field)."""
    return metadata.has_experiment if isinstance(metadata, FlagMetadata) else None


class _Lane:
    """A capture lane: a queue drained by its own consumer pool, posting to one endpoint.

    Internal and unexported. The client owns one lane per traffic class
    (analytics, AI) so each gets its own backpressure, flush cadence,
    per-event size cap, and wire protocol without lane-conditional branches
    in shared consumer code.
    """

    log = logging.getLogger("posthog")

    def __init__(
        self,
        *,
        name,
        api_key,
        host,
        on_error,
        max_queue_size,
        thread_count,
        send,
        flush_at,
        flush_interval,
        gzip,
        max_retries,
        timeout,
        historical_migration,
        endpoint,
        max_msg_size,
        capture_mode,
        capture_compression,
        eager_start,
    ):
        self.name = name
        self.api_key = api_key
        self.host = host
        self.on_error = on_error
        self.send = send
        self.flush_at = flush_at
        self.flush_interval = flush_interval
        self.gzip = gzip
        self.max_retries = max_retries
        self.timeout = timeout
        self.historical_migration = historical_migration
        self.endpoint = endpoint
        self.max_msg_size = max_msg_size
        self.capture_mode = capture_mode
        self.capture_compression = capture_compression
        self._max_queue_size = max_queue_size
        self._thread_count = thread_count
        self._eager_start = eager_start
        self.queue: Queue = _new_lane_queue(max_queue_size)
        self.available = not isinstance(self.queue, _DisabledLaneQueue)
        self.consumers: List[Consumer] = []
        self._started = False
        self._closed = False
        self._active_sync_sends = 0
        self._start_lock = threading.Lock()
        self._sync_sends_done = threading.Condition(self._start_lock)
        self._drain_signal = _DrainSignal(self.queue)
        if eager_start and self.available:
            self.start()

    def _start_locked(self) -> None:
        if self._started or self._closed or not self.available:
            return
        for _ in range(self._thread_count):
            consumer = Consumer(
                self.queue,
                self.api_key,
                host=self.host,
                on_error=self.on_error,
                flush_at=self.flush_at,
                flush_interval=self.flush_interval,
                gzip=self.gzip,
                retries=self.max_retries,
                timeout=self.timeout,
                historical_migration=self.historical_migration,
                endpoint=self.endpoint,
                max_msg_size=self.max_msg_size,
                capture_mode=self.capture_mode,
                capture_compression=self.capture_compression,
            )
            consumer._set_drain_signal(self._drain_signal)
            self.consumers.append(consumer)

            if self.send:
                consumer.start()
        self._started = True

    def start(self):
        """Construct this lane's consumer pool, starting its threads when sending is enabled.

        Idempotent and thread-safe, so concurrent first captures start exactly
        one pool.
        """
        with self._start_lock:
            self._start_locked()

    def enqueue(self, msg) -> bool:
        """Atomically admit and queue `msg`, starting the lane on its first event."""
        with self._start_lock:
            if self._closed or not self.available:
                return False
            self._start_locked()
            try:
                self.queue.put(msg, block=False)
                return True
            except Full:
                return False

    def run_sync_if_open(self, send) -> bool:
        """Run a synchronous send admitted before closure, and report whether it ran."""
        with self._sync_sends_done:
            if self._closed:
                return False
            self._active_sync_sends += 1

        try:
            send()
        finally:
            with self._sync_sends_done:
                self._active_sync_sends -= 1
                if not self._active_sync_sends:
                    self._sync_sends_done.notify_all()
        return True

    def close(self) -> None:
        """Terminal: atomically refuse all future queue and sync admissions."""
        with self._start_lock:
            self._closed = True

    def wait_for_sync_sends(self) -> None:
        """Wait for synchronous sends admitted before close to finish."""
        with self._sync_sends_done:
            while self._active_sync_sends:
                self._sync_sends_done.wait()

    def flush(self, timeout_seconds: Optional[float]) -> None:
        """Block until this lane's queue drains, or until `timeout_seconds` elapse.

        Signals the consumers first so a partial batch is delivered now instead
        of waiting out `flush_at` / `flush_interval`.
        """
        queue = self.queue
        # Keep the request active only while this flush is waiting. This avoids
        # an empty flush changing how events captured after it are batched.
        self._drain_signal.request()
        try:
            size = queue.qsize()
            deadline = (
                None if timeout_seconds is None else time.monotonic() + timeout_seconds
            )
            while queue.unfinished_tasks:
                if deadline is None and not any(
                    consumer.is_alive() for consumer in self.consumers
                ):
                    self.discard_undrainable_queued_work()
                    break
                with queue.all_tasks_done:
                    if not queue.unfinished_tasks:
                        break
                    if deadline is None:
                        wait_seconds = 0.05
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.log.warning(
                                "%s lane flush ran out of budget (%.1fs granted) with %s items pending.",
                                self.name,
                                timeout_seconds,
                                queue.unfinished_tasks,
                            )
                            return
                        wait_seconds = min(0.05, remaining)
                    queue.all_tasks_done.wait(wait_seconds)

            # Note that this message may not be precise, because of threading.
            self.log.debug("successfully flushed about %s items.", size)
        finally:
            self._drain_signal.complete()

    def discard_undrainable_queued_work(self) -> None:
        """Balance queued work when this lane has no running sender."""
        if any(consumer.is_alive() for consumer in self.consumers):
            return

        dropped = 0
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break
            self.queue.task_done()
            dropped += 1
        if dropped:
            self.log.warning(
                "%s lane discarded %d queued events because no consumer is running",
                self.name,
                dropped,
            )

    def join(self) -> None:
        """Pause this lane's consumers and wait for them to exit."""
        # Normal teardown bypasses the batching wait so a partial batch is sent.
        errors: list[Exception] = []
        drain_requested = False
        try:
            self._drain_signal.request()
            drain_requested = True
        except Exception as error:
            self.log.exception(
                "Failed to request %s lane drain during lifecycle cleanup", self.name
            )
            errors.append(error)

        for consumer in self.consumers:
            try:
                consumer._pause(drain=True)
            except Exception as error:
                self.log.exception(
                    "Failed to pause %s lane consumer during lifecycle cleanup",
                    self.name,
                )
                errors.append(error)
        for consumer in self.consumers:
            try:
                consumer.join()
            except RuntimeError:
                # consumer thread has not started
                pass
            except Exception as error:
                self.log.exception(
                    "Failed to join %s lane consumer during lifecycle cleanup",
                    self.name,
                )
                errors.append(error)
        try:
            self.discard_undrainable_queued_work()
        except Exception as error:
            self.log.exception(
                "Failed to discard queued %s lane work during lifecycle cleanup",
                self.name,
            )
            errors.append(error)
        if drain_requested:
            try:
                self._drain_signal.complete()
            except Exception as error:
                self.log.exception(
                    "Failed to complete %s lane drain during lifecycle cleanup",
                    self.name,
                )
                errors.append(error)

        if errors:
            raise errors[0]

    def reset_sync_send_state_after_fork(self) -> None:
        """Replace sync-send state inherited from threads that did not survive fork."""
        self._active_sync_sends = 0
        self._start_lock = threading.Lock()
        self._sync_sends_done = threading.Condition(self._start_lock)

    def rebuild_after_fork(self, *, closed: bool) -> None:
        """Replace fork-unsafe lane state in a forked child.

        Threads do not survive fork() and queue.Queue internal locks may be in
        an inconsistent state, so the queue, lock, and consumer pool are
        replaced. Inherited queue items are not retained as they'll be handled
        by the parent process's consumers. ``closed`` normalizes every lane to
        the client's fork-visible lifecycle state. An eager open lane restarts
        immediately; a lazy lane returns to not-started and restarts on next use.
        """
        self.queue = _new_lane_queue(self._max_queue_size)
        self.available = not isinstance(self.queue, _DisabledLaneQueue)
        self.reset_sync_send_state_after_fork()
        self._drain_signal = _DrainSignal(self.queue)
        self.consumers = []
        self._started = False
        self._closed = closed
        if self._eager_start and self.available:
            self.start()


class Client(object):
    """
    This is the SDK reference for the PostHog Python SDK.
    You can learn more about example usage in the [Python SDK documentation](/docs/libraries/python).
    You can also follow [Flask](/docs/libraries/flask) and [Django](/docs/libraries/django)
    guides to integrate PostHog into your project.

    For long-running applications, create one client during application startup
    and reuse it for the lifetime of the process. This keeps background queues
    predictable and makes shutdown flushing straightforward. Multiple clients are
    still supported for intentional multi-project or multi-host setups.

    Examples:
        ```python
        from posthog import Posthog
        posthog = Posthog('<ph_project_api_key>', host='<ph_client_api_host>')
        posthog.debug = True
        if settings.TEST:
            posthog.disabled = True
        ```
    """

    log = logging.getLogger("posthog")
    _client_registry_lock = threading.Lock()
    _client_registry_pid = os.getpid()
    _client_registry: dict[tuple[str, str], weakref.WeakSet] = {}
    _duplicate_client_warnings: set[tuple[str, str]] = set()

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
        feature_flags_request_max_retries=1,
        super_properties=None,
        enable_exception_autocapture=False,
        log_captured_exceptions=False,
        project_root=None,
        privacy_mode=False,
        before_send=None,
        flag_fallback_cache_url=None,
        enable_local_evaluation=True,
        flag_definition_cache_provider: Optional[FlagDefinitionCacheProvider] = None,
        capture_exception_code_variables=False,
        code_variables_mask_patterns=None,
        code_variables_ignore_patterns=None,
        code_variables_mask_url_credentials=None,
        code_variables_detect_secrets=None,
        in_app_modules: list[str] | None = None,
        enable_exception_autocapture_rate_limiting=False,
        exception_autocapture_bucket_size=ExceptionCapture.DEFAULT_BUCKET_SIZE,
        exception_autocapture_refill_rate=ExceptionCapture.DEFAULT_REFILL_RATE,
        exception_autocapture_refill_interval_seconds=ExceptionCapture.DEFAULT_REFILL_INTERVAL_SECONDS,
        capture_mode: Optional[Union[CaptureMode, str]] = None,
        capture_compression: Optional[Union[CaptureCompression, str]] = None,
        secret_key=None,
        metrics: Optional[dict] = None,
        enable_full_ai_capture=False,
        # Appended rather than grouped with the other `capture_*` options so
        # existing positional arguments keep their slots.
        capture_trace_context=False,
        _use_ai_lane=False,
        _enable_multimodal_capture=False,
    ):
        """
        Initialize a new PostHog client instance.

        Args:
            project_api_key: PostHog project API key/token.
            host: PostHog host. Defaults to the US ingestion endpoint when not
                set. App hosts such as ``https://us.posthog.com`` are mapped to
                the corresponding ingestion host.
            debug: Enable verbose SDK logging and re-raise errors from public
                API methods.
            max_queue_size: Maximum number of events buffered before upload.
            send: If False, queueing succeeds but events are not sent.
            on_error: Optional callback invoked by background consumers when an
                upload fails. Keep it short and non-blocking. Calling lifecycle
                methods directly is safe and deferred, but do not start another
                thread or task that calls ``flush()``, ``join()``, or
                ``shutdown()`` and then wait for it from the callback.
            flush_at: Number of queued events that triggers a batch upload.
            flush_interval: Maximum seconds a background consumer waits before
                flushing a partial batch.
            gzip: Whether to gzip event upload payloads.
            max_retries: Number of upload retries. Values below 0 are treated as 0.
            sync_mode: If True, send each event synchronously instead of using
                background worker threads.
            timeout: HTTP request timeout in seconds for event uploads.
            thread: Number of background consumer threads.
            poll_interval: Seconds between local feature flag definition refreshes.
            secret_key: A Personal API Key or Project Secret API Key, used to
                authenticate local feature flag evaluation, remote config
                payloads, and decrypted flag payloads. Example::

                    posthog.Client(project_api_key, secret_key="phx_...")

            personal_api_key: Deprecated alias for ``secret_key``. Still honored
                for backwards compatibility; prefer ``secret_key``, which also
                accepts a Project Secret API Key.
            disabled: If True, disable captures and API requests. Useful in tests.
            disable_geoip: Whether to disable server-side GeoIP enrichment.
                Defaults to True.
            is_server: Whether events are emitted from a server-side runtime.
                Defaults to True; set to False when using the SDK as a client/CLI
                so the device OS is attributed to the person normally.
            historical_migration: Mark events as historical migration imports.
            feature_flags_request_timeout_seconds: Timeout in seconds for feature
                flag and remote config requests.
            feature_flags_request_max_retries: Number of retries for feature flag
                requests after network, transport, or timeout failures. Defaults
                to 1. Set to 0 to disable retries.
            super_properties: Properties merged into every captured event.
            enable_exception_autocapture: Automatically capture uncaught
                exceptions.
            log_captured_exceptions: Also log exceptions captured by error
                tracking.
            project_root: Root path used to determine in-app stack frames for
                captured exceptions. Defaults to the current working directory.
            privacy_mode: For AI observability, capture usage metadata without
                prompt inputs or outputs.
            enable_full_ai_capture: Route PostHog AI wrapper events through
                the dedicated AI capture endpoint and capture full AI content:
                skips string truncation and passes media (base64/data URIs)
                through unredacted. ``privacy_mode`` always wins. Defaults to
                False.
            before_send: Optional callback that can modify or drop events before
                upload. Return ``None`` to drop an event.
            flag_fallback_cache_url: Optional feature flag fallback cache URL,
                such as ``memory://local/?ttl=300&size=10000`` or a Redis URL.
            enable_local_evaluation: Whether to poll feature flag definitions for
                local evaluation when a personal API key is configured.
            flag_definition_cache_provider: Optional external cache provider for
                sharing feature flag definitions across workers.
            capture_exception_code_variables: Capture local variable values on
                exception stack frames.
            capture_trace_context: When OpenTelemetry is installed and a valid span is
                active at capture time, add its trace and span IDs as ``$trace_id`` and
                ``$span_id`` properties to events captured with ``capture()`` and
                ``capture_ai()``, so they can be correlated with backend traces. Explicit
                ``$trace_id``/``$span_id`` values passed in ``properties`` win. Exception
                events (``capture_exception``) always attach these IDs regardless of this
                setting. Defaults to False.
            code_variables_mask_patterns: Variable-name patterns to mask when
                capturing code variables.
            code_variables_ignore_patterns: Variable-name patterns to omit when
                capturing code variables.
            code_variables_mask_url_credentials: Scrub credentials embedded in
                URLs/DSNs (e.g. ``user:pass@host``) from captured code variables,
                regardless of the surrounding variable name. Defaults to True.
            code_variables_detect_secrets: Last-resort entropy-based detection that
                redacts high-entropy secret-looking values (API keys, tokens, strong
                passwords) sitting in innocuously-named variables, after the name and
                URL checks. Skips structured ids (UUIDs, ObjectIds, hashes). Defaults
                to True.
            in_app_modules: Module/package prefixes treated as in-app frames in
                captured exceptions.
            enable_exception_autocapture_rate_limiting: Rate limit
                autocaptured exceptions client-side with a token bucket per
                exception type. Disabled by default.
            exception_autocapture_bucket_size: Maximum burst of autocaptured
                exceptions allowed per exception type (token bucket size,
                clamped to 0-100).
            exception_autocapture_refill_rate: Tokens restored per refill
                interval for each exception type's bucket.
            exception_autocapture_refill_interval_seconds: Seconds between
                token refills for autocaptured exception rate limiting.
            capture_mode: Capture wire protocol to use. Defaults to
                ``CaptureMode.V0`` (legacy ``/batch/``). Set ``CaptureMode.V1``
                (or pass the string ``"v1"``) to opt into
                ``/i/v1/analytics/events``. When omitted, the
                ``POSTHOG_CAPTURE_MODE`` env var is consulted, then ``V0``.
            capture_compression: Request-body compression for capture-v1 uploads
                (ignored in V0, which uses ``gzip``). ``CaptureCompression.GZIP``
                or ``DEFLATE`` (or the strings ``"gzip"``/``"deflate"``). When
                omitted, the ``POSTHOG_CAPTURE_COMPRESSION`` env var is consulted,
                then the legacy ``gzip`` flag, then no compression.

        Examples:
            ```python
            from posthog import Posthog

            posthog = Posthog('<ph_project_api_key>', host='<ph_app_host>')
            ```

        Category:
            Initialization
        """
        # api_key: This should be the Team API Key (token), public
        self.api_key = (project_api_key or "").strip()

        self.on_error = on_error
        self.debug = debug
        self.send = send
        self.sync_mode = sync_mode
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_condition = threading.Condition(self._lifecycle_lock)
        self._lifecycle_owner: Optional[threading.Thread] = None
        self._workers_joined = False
        self._join_cleanup_complete = False
        self._join_requested = False
        self._shutdown_requested = False
        self._shutdown_complete = False
        self._lifecycle_cleanup_failed = False
        self._deferred_lifecycle_thread_pending = False
        self._deferred_lifecycle_dirty = False
        self._lifecycle_callback_context: ContextVar[bool] = ContextVar(
            "posthog_lifecycle_callback", default=False
        )
        self._deferred_flush_lock = threading.Lock()
        self._deferred_flush_pending = False
        self._deferred_flush_followup = False
        self._deferred_flush_followup_timeout: Optional[float] = None
        # Used for session replay URL generation - we don't want the server host here.
        self.raw_host = normalize_host(host)
        self.host = determine_server_host(host)
        self._duplicate_client_registry_key: Optional[tuple[str, str]] = None
        self.gzip = gzip
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._feature_flags: Optional[list[Any]] = (
            None  # private variable to store flags
        )
        self.feature_flags_by_key: Optional[dict[str, Any]] = None
        self.group_type_mapping: Optional[dict[str, str]] = None
        self.cohorts: Optional[dict[str, Any]] = None
        self.poll_interval = poll_interval
        self.feature_flags_request_timeout_seconds = (
            feature_flags_request_timeout_seconds
        )
        self.feature_flags_request_max_retries = max(
            0, feature_flags_request_max_retries
        )
        self.poller: Optional[Poller] = None
        self.distinct_ids_feature_flags_reported = SizeLimitedDict(MAX_DICT_SIZE, set)
        self.flag_fallback_cache_url = flag_fallback_cache_url
        self.flag_cache = self._initialize_flag_cache(flag_fallback_cache_url)
        self.flag_definition_version = 0
        self._flags_etag: Optional[str] = None
        self._flag_definition_fetch_generation = 0
        self._flag_definition_published_generation = 0
        self._flag_definition_cache_generation = 0
        self._flag_definition_publication_lock = threading.Lock()
        self._flag_definition_cache_write_lock = threading.RLock()
        self._flag_definition_cache_provider = flag_definition_cache_provider
        self._flag_definition_cache_provider_async_runner: Optional[
            _BackgroundEventLoopRunner
        ] = None
        self._flag_definition_cache_provider_async_runner_lock = threading.Lock()
        self.disabled = disabled or not self.api_key
        self.disable_geoip = disable_geoip
        self._metrics_config = metrics
        self._metrics: Optional[PostHogMetrics] = None
        self._metrics_lock = threading.Lock()
        # `_use_ai_lane` / `_enable_multimodal_capture` are deprecated aliases.
        self.enable_full_ai_capture = (
            enable_full_ai_capture is True
            or _use_ai_lane is True
            or _enable_multimodal_capture is True
        )
        self.is_server = is_server
        self.historical_migration = historical_migration
        # Selects the capture wire protocol (V0 legacy `/batch/` vs V1
        # `/i/v1/analytics/events`). Resolved here so the env-var fallback is
        # applied once; V0 is the default and keeps upgrades transparent.
        self.capture_mode = _resolve_capture_mode(capture_mode)
        # v1-only request compression; falls back to the legacy `gzip` flag when
        # neither the kwarg nor POSTHOG_CAPTURE_COMPRESSION is set.
        self.capture_compression = _resolve_capture_compression(
            capture_compression, gzip_fallback=gzip
        )
        self.super_properties = super_properties
        self.enable_exception_autocapture = enable_exception_autocapture
        self.log_captured_exceptions = log_captured_exceptions
        self.enable_exception_autocapture_rate_limiting = (
            enable_exception_autocapture_rate_limiting
        )
        self.exception_autocapture_bucket_size = exception_autocapture_bucket_size
        self.exception_autocapture_refill_rate = exception_autocapture_refill_rate
        self.exception_autocapture_refill_interval_seconds = (
            exception_autocapture_refill_interval_seconds
        )
        self.exception_capture = None
        self.privacy_mode = privacy_mode
        self.enable_local_evaluation = enable_local_evaluation
        # Server-controlled gate for minimal $feature_flag_called events, read from
        # the /flags v2 response and the local-evaluation payload. False until the
        # server reports it, so full events are the fail-safe.
        self._minimal_flag_called_events: bool = False

        self.capture_trace_context = capture_trace_context
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

        if project_root is None:
            try:
                project_root = os.getcwd()
            except Exception:
                project_root = None

        self.project_root = project_root

        if personal_api_key is not None and secret_key is None:
            warnings.warn(
                "`personal_api_key` is deprecated; use `secret_key` instead. "
                "`secret_key` accepts a Personal API Key or a Project Secret API Key.",
                DeprecationWarning,
                stacklevel=2,
            )
        elif secret_key is not None and personal_api_key is not None:
            self.log.warning(
                "[FEATURE FLAGS] Both `secret_key` and `personal_api_key` were "
                "provided; using `secret_key` and ignoring `personal_api_key`."
            )
        resolved_secret_key = secret_key if secret_key is not None else personal_api_key
        self.secret_key = (
            resolved_secret_key.strip()
            if isinstance(resolved_secret_key, str)
            else resolved_secret_key
        ) or None
        self.personal_api_key = self.secret_key
        if debug:
            # Ensures that debug level messages are logged when debug mode is on.
            # Otherwise, defaults to WARNING level. See https://docs.python.org/3/howto/logging.html#what-happens-if-no-configuration-is-provided
            logging.basicConfig()
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.WARNING)

        if not self.api_key:
            self.log.error(
                "api_key is empty after trimming whitespace; check your project API key"
            )

        self._set_before_send(before_send)

        if self.enable_exception_autocapture:
            self.exception_capture = ExceptionCapture(
                self,
                rate_limiting_enabled=self.enable_exception_autocapture_rate_limiting,
                bucket_size=self.exception_autocapture_bucket_size,
                refill_rate=self.exception_autocapture_refill_rate,
                refill_interval_seconds=self.exception_autocapture_refill_interval_seconds,
            )

        if not sync_mode and send:
            # On program exit, allow the consumer threads to exit cleanly.
            # This prevents exceptions and a messy shutdown when the
            # interpreter is destroyed before the daemon threads finish
            # execution. Exit performs only a short best-effort flush; call
            # flush() or shutdown() explicitly when blocking completion matters.
            atexit.register(self._atexit)

        lane_defaults = dict(
            api_key=self.api_key,
            host=self.host,
            on_error=on_error,
            max_queue_size=max_queue_size,
            thread_count=thread,
            send=send,
            flush_at=flush_at,
            flush_interval=flush_interval,
            gzip=gzip,
            max_retries=self.max_retries,
            timeout=timeout,
            historical_migration=historical_migration,
        )
        self._analytics_lane = _Lane(
            name="analytics",
            **lane_defaults,
            endpoint=EVENTS_ENDPOINT,
            max_msg_size=MAX_MSG_SIZE,
            capture_mode=self.capture_mode,
            capture_compression=self.capture_compression,
            eager_start=not sync_mode,
        )
        # The AI lane is pinned to the v0 submitter: the AI endpoint has no v1
        # form, and this keeps multi-MB AI events away from capture v1's
        # smaller caps. The `capture_compression` pin is inert on v0 — its wire
        # compression is the `gzip` flag, inherited from client config. Lazy
        # start, so the many clients that never emit AI events pay for no
        # extra threads.
        self._ai_lane = _Lane(
            name="ai",
            **lane_defaults,
            endpoint=AI_EVENTS_ENDPOINT,
            max_msg_size=AI_MAX_MSG_SIZE,
            capture_mode=CaptureMode.V0,
            capture_compression=CaptureCompression.NONE,
            eager_start=False,
        )
        self._lanes = [self._analytics_lane, self._ai_lane]

        if hasattr(os, "register_at_fork"):
            weak_self = weakref.ref(self)
            os.register_at_fork(
                after_in_child=lambda: Client._reinit_after_fork_weak(weak_self)
            )

        self._warn_if_duplicate_async_client()

    @property
    def queue(self) -> Queue:
        """The analytics lane's queue (kept for backwards compatibility)."""
        return self._analytics_lane.queue

    @property
    def consumers(self) -> Optional[List[Consumer]]:
        """Flat list of the lanes' consumers, analytics first (kept for backwards compatibility)."""
        if self.sync_mode:
            return None
        return [consumer for lane in self._lanes for consumer in lane.consumers]

    @property
    def _use_ai_lane(self) -> bool:
        """Deprecated alias for `enable_full_ai_capture`."""
        return self.enable_full_ai_capture

    @_use_ai_lane.setter
    def _use_ai_lane(self, value) -> None:
        self.enable_full_ai_capture = value is True

    @property
    def _enable_multimodal_capture(self) -> bool:
        """Deprecated alias for `enable_full_ai_capture`."""
        return self.enable_full_ai_capture

    @_enable_multimodal_capture.setter
    def _enable_multimodal_capture(self, value) -> None:
        self.enable_full_ai_capture = value is True

    def _warn_if_duplicate_async_client(self):
        if self.disabled or not self.send or self.sync_mode or not self.api_key:
            return

        registry_key = (self.api_key, self.host)
        should_warn = False

        with Client._client_registry_lock:
            clients = Client._client_registry.setdefault(
                registry_key, weakref.WeakSet()
            )
            has_existing_client = len(clients) > 0
            clients.add(self)
            self._duplicate_client_registry_key = registry_key

            if (
                has_existing_client
                and registry_key not in Client._duplicate_client_warnings
            ):
                Client._duplicate_client_warnings.add(registry_key)
                should_warn = True

        if should_warn:
            self.log.warning(
                "Multiple active PostHog clients detected for the same project "
                "API key and host. Reuse one Posthog instance per app or "
                "process when possible to avoid competing background queues "
                "and missed shutdown flushes. Multiple clients are supported "
                "when intentional."
            )

    def _unregister_duplicate_client(self):
        registry_key = self._duplicate_client_registry_key
        if registry_key is None:
            return

        with Client._client_registry_lock:
            clients = Client._client_registry.get(registry_key)
            if clients is not None:
                clients.discard(self)
                if not clients:
                    del Client._client_registry[registry_key]
                    Client._duplicate_client_warnings.discard(registry_key)

            self._duplicate_client_registry_key = None

    def _set_before_send(self, before_send):
        if before_send is not None:
            if callable(before_send):
                self.before_send = before_send
            else:
                self.log.warning("before_send is not callable, it will be ignored")
                self.before_send = None
        else:
            self.before_send = None

    def new_context(self, fresh=False, capture_exceptions: Optional[bool] = None):
        """
        Create a new context for managing shared state. Learn more about [contexts](/docs/libraries/python#contexts).

        Args:
            fresh: Whether to create a fresh context that doesn't inherit from parent.
            capture_exceptions: Whether to automatically capture exceptions in this context. If omitted, defaults to this client's exception autocapture setting.

        Examples:
            ```python
            with client.new_context():
                client.identify_context('<distinct_id>')
                client.capture('event_name')
            ```

        Category:
            Contexts
        """
        return new_context(
            fresh=fresh, capture_exceptions=capture_exceptions, client=self
        )

    def scoped(self, fresh=False, capture_exceptions: Optional[bool] = None):
        """
        Decorator that creates a new context for the wrapped function using this client.

        Args:
            fresh: Whether to create a fresh context that doesn't inherit from parent.
            capture_exceptions: Whether to automatically capture exceptions in this context. If omitted, defaults to this client's exception autocapture setting.

        Category:
            Contexts
        """

        return _context_scoped(
            fresh=fresh, capture_exceptions=capture_exceptions, client=self
        )

    def tag(self, name: str, value: Any) -> None:
        """
        Add a tag to the current context.

        Args:
            name: The tag key.
            value: The tag value.

        Category:
            Contexts
        """
        _context_tag(name, value)

    def get_tags(self) -> Dict[str, Any]:
        """
        Get all tags from the current context.

        Returns:
            Dict of all tags in the current context.

        Category:
            Contexts
        """
        return _context_get_tags()

    def identify_context(self, distinct_id: str) -> None:
        """
        Identify the current context with a distinct ID.

        Args:
            distinct_id: The distinct ID to associate with the current context and its children.

        Category:
            Contexts
        """
        _context_identify_context(distinct_id)

    def set_context_session(self, session_id: str) -> None:
        """
        Set the session ID for the current context.

        Args:
            session_id: The session ID to associate with the current context and its children.

        Category:
            Contexts
        """
        _context_set_context_session(session_id)

    def set_context_device_id(self, device_id: str) -> None:
        """
        Set the device ID for the current context.

        Args:
            device_id: The device ID to associate with the current context and its children.

        Category:
            Contexts
        """
        _context_set_context_device_id(device_id)

    @property
    def feature_flags(self):
        """
        Get the local evaluation feature flags.
        """
        return self._feature_flags

    @feature_flags.setter
    def feature_flags(self, flags):
        """
        Set the local evaluation feature flags.
        """
        self._feature_flags = flags or []
        self.feature_flags_by_key = {
            flag["key"]: flag
            for flag in self._feature_flags
            if flag.get("key") is not None
        }
        assert self.feature_flags_by_key is not None, (
            "feature_flags_by_key should be initialized when feature_flags is set"
        )

    def get_feature_variants(
        self,
        distinct_id: ID_TYPES,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> dict[str, Union[bool, str]]:
        """
        Get feature flag variants for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.
            flag_keys_to_evaluate: A list of specific flag keys to evaluate. If provided,
                only these flags will be evaluated, improving performance.
            device_id: The device ID for this request.

        Category:
            Feature flags
        """
        resp_data = self.get_flags_decision(
            distinct_id,
            groups,
            person_properties,
            group_properties,
            disable_geoip,
            flag_keys_to_evaluate,
            device_id=device_id,
        )
        return to_values(resp_data) or {}

    def get_feature_payloads(
        self,
        distinct_id: ID_TYPES,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> dict[str, str]:
        """
        Get feature flag payloads for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.
            flag_keys_to_evaluate: A list of specific flag keys to evaluate. If provided,
                only these flags will be evaluated, improving performance.
            device_id: The device ID for this request.

        Examples:
            ```python
            payloads = posthog.get_feature_payloads('<distinct_id>')
            ```

        Category:
            Feature flags
        """
        resp_data = self.get_flags_decision(
            distinct_id,
            groups,
            person_properties,
            group_properties,
            disable_geoip,
            flag_keys_to_evaluate,
            device_id=device_id,
        )
        return to_payloads(resp_data) or {}

    def get_feature_flags_and_payloads(
        self,
        distinct_id: ID_TYPES,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsAndPayloads:
        """
        Get feature flags and payloads for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.
            flag_keys_to_evaluate: A list of specific flag keys to evaluate. If provided,
                only these flags will be evaluated, improving performance.
            device_id: The device ID for this request.

        Examples:
            ```python
            result = posthog.get_feature_flags_and_payloads('<distinct_id>')
            ```

        Category:
            Feature flags
        """
        resp = self.get_flags_decision(
            distinct_id,
            groups,
            person_properties,
            group_properties,
            disable_geoip,
            flag_keys_to_evaluate,
            device_id=device_id,
        )
        return to_flags_and_payloads(resp)

    def get_flags_decision(
        self,
        distinct_id: Optional[ID_TYPES] = None,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsResponse:
        """
        Get feature flags decision.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.
            flag_keys_to_evaluate: A list of specific flag keys to evaluate. If provided,
                only these flags will be evaluated, improving performance.
            device_id: The device ID for this request.

        Examples:
            ```python
            decision = posthog.get_flags_decision('user123')
            ```

        Category:
            Feature flags
        """
        try:
            return self._get_flags_decision(
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

    def _get_flags_decision(
        self,
        distinct_id: Optional[ID_TYPES] = None,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsResponse:
        if self.disabled:
            return normalize_flags_response({})

        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        if distinct_id is None:
            distinct_id = get_context_distinct_id()

        if device_id is None:
            device_id = get_context_device_id()

        if disable_geoip is None:
            disable_geoip = self.disable_geoip

        if not groups:
            groups = {}

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

        resp_data = flags(
            self.api_key,
            self.host,
            timeout=self.feature_flags_request_timeout_seconds,
            max_retries=self.feature_flags_request_max_retries,
            **request_data,
        )

        response = normalize_flags_response(resp_data)
        # Server-controlled gate for minimal $feature_flag_called events. Only the
        # v2 response shape carries it; absent (legacy shape, older server, team
        # not gated) means False.
        self._minimal_flag_called_events = (
            response.get("minimalFlagCalledEvents") is True
        )
        return response

    @no_throw()
    def capture(
        self, event: str, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        """
        Captures an event manually. [Learn about capture best practices](https://posthog.com/docs/product-analytics/capture-events)

        Args:
            event: The event name to capture.
            distinct_id: The distinct ID of the user.
            properties: A dictionary of properties to include with the event.
            timestamp: The timestamp of the event. UTC is preferred; non-UTC
                datetimes and parseable ISO timestamp strings are converted to UTC.
            uuid: A unique identifier for the event. If provided, it must be a
                valid UUID string or uuid.UUID instance; invalid values are
                ignored and replaced with a newly generated UUID.
            groups: A dictionary of group information.
            flags: A FeatureFlagEvaluations snapshot from evaluate_flags(). The
                exact values from the snapshot are attached with no extra /flags
                request.
            send_feature_flags: Deprecated. Prefer flags=... from
                evaluate_flags(). When truthy, evaluates flags during capture and
                attaches them to the event.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            # Anonymous event
            posthog.capture('some-anon-event')
            ```
            ```python
            # Context usage
            from posthog import identify_context, new_context
            with new_context():
                identify_context('distinct_id_of_the_user')
                posthog.capture('user_signed_up')
                posthog.capture('user_logged_in')
                posthog.capture('some-custom-action', distinct_id='distinct_id_of_the_user')
            ```
            ```python
            # Set event properties
            posthog.capture(
                "user_signed_up",
                distinct_id="distinct_id_of_the_user",
                properties={
                    "login_type": "email",
                    "is_free_trial": "true"
                }
            )
            ```
            ```python
            # Page view event
            posthog.capture('$pageview', distinct_id="distinct_id_of_the_user", properties={'$current_url': 'https://example.com'})
            ```

        Category:
            Capture
        """
        return self._capture(event, self._analytics_lane, **kwargs)

    @no_throw()
    def capture_ai(
        self, event: str, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        """Capture an AI event on the dedicated AI capture endpoint.

        Beta: the signature is stable; operational limits (per-event size
        cap, batching, endpoint) may change without notice.

        Takes the same arguments and returns the same value as `capture()`:
        the event UUID, or None when the event was not admitted (disabled
        client, or dropped by `before_send`). The event is queued on an
        isolated AI lane with its own consumer pool and a higher per-event
        size cap, posting to the dedicated AI ingestion endpoint. The payload
        is sent as given — no redaction or truncation is applied here.

        Category:
            Capture
        """
        if not event.startswith("$ai_"):
            self.log.debug(
                "capture_ai called with non-AI event name %r; routing it to the AI endpoint anyway.",
                event,
            )
        return self._capture(event, self._ai_lane, **kwargs)

    def _capture(
        self, event: str, lane: _Lane, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        """Shared message-building body of `capture()` and `capture_ai()`; `lane` picks the wire destination."""
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        timestamp = kwargs.get("timestamp", None)
        uuid = kwargs.get("uuid", None)
        groups = kwargs.get("groups", None)
        flags_snapshot = kwargs.get("flags", None)
        send_feature_flags = kwargs.get("send_feature_flags", False)
        disable_geoip = kwargs.get("disable_geoip", None)
        # Internal, set for minimal $feature_flag_called events: a strict allowlist
        # applied to the fully-enriched properties dict just before enqueueing.
        property_allowlist = kwargs.get("_property_allowlist", None)

        properties = {**(properties or {}), **system_context()}

        if self.capture_trace_context:
            properties = {**_get_current_otel_span_properties(), **properties}

        properties = add_context_tags(properties)
        assert properties is not None  # Type hint for mypy

        (distinct_id, personless) = get_identity_state(distinct_id)

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

        # Precedence: an explicit ``flags`` snapshot always wins, regardless of
        # ``send_feature_flags``. The snapshot guarantees the event carries the same
        # values the developer branched on with no additional network call. The
        # ``send_feature_flags`` path only runs when no snapshot is provided.
        if flags_snapshot is not None:
            if send_feature_flags:
                self.log.warning(
                    "[FEATURE FLAGS] Both `flags` and `send_feature_flags` were passed to "
                    "capture(); using `flags` and ignoring `send_feature_flags`."
                )
            extra_properties = flags_snapshot._get_event_properties()
        else:
            feature_variants: Optional[dict[str, Union[bool, str]]] = {}

            # Parse and normalize send_feature_flags parameter
            flag_options = self._parse_send_feature_flags(send_feature_flags)

            if flag_options["should_send"]:
                warnings.warn(
                    "`send_feature_flags` is deprecated and will be removed in a future major "
                    "version. Pass a `flags` snapshot from `posthog.evaluate_flags(...)` instead "
                    "— it avoids a second `/flags` request per capture and guarantees the event "
                    "carries the exact flag values your code branched on.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                try:
                    if flag_options["only_evaluate_locally"] is True:
                        # Local evaluation explicitly requested
                        feature_variants = self.get_all_flags(
                            distinct_id,
                            groups=(groups or {}),
                            person_properties=flag_options["person_properties"],
                            group_properties=flag_options["group_properties"],
                            disable_geoip=disable_geoip,
                            only_evaluate_locally=True,
                            flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                        )
                    elif flag_options["only_evaluate_locally"] is False:
                        # Remote evaluation explicitly requested
                        feature_variants = self.get_feature_variants(
                            distinct_id,
                            groups,
                            person_properties=flag_options["person_properties"],
                            group_properties=flag_options["group_properties"],
                            disable_geoip=disable_geoip,
                            flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                        )
                    elif self.feature_flags:
                        # Local flags available, prefer local evaluation
                        feature_variants = self.get_all_flags(
                            distinct_id,
                            groups=(groups or {}),
                            person_properties=flag_options["person_properties"],
                            group_properties=flag_options["group_properties"],
                            disable_geoip=disable_geoip,
                            only_evaluate_locally=True,
                            flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                        )
                    else:
                        # Fall back to remote evaluation
                        feature_variants = self.get_feature_variants(
                            distinct_id,
                            groups,
                            person_properties=flag_options["person_properties"],
                            group_properties=flag_options["group_properties"],
                            disable_geoip=disable_geoip,
                            flag_keys_to_evaluate=flag_options["flag_keys_filter"],
                        )
                except Exception as e:
                    self.log.exception(
                        f"[FEATURE FLAGS] Unable to get feature variants: {e}"
                    )

            for feature, variant in (feature_variants or {}).items():
                extra_properties[f"$feature/{feature}"] = variant

            active_feature_flags = [
                key
                for (key, value) in (feature_variants or {}).items()
                if value is not False
            ]
            if active_feature_flags:
                extra_properties["$active_feature_flags"] = active_feature_flags

        if extra_properties:
            properties = {**extra_properties, **properties}
            msg["properties"] = properties

        return self._enqueue(
            msg, disable_geoip, lane, property_allowlist=property_allowlist
        )

    def _parse_send_feature_flags(self, send_feature_flags) -> SendFeatureFlagsOptions:
        """
        Parse and normalize send_feature_flags parameter into a standard format.

        Args:
            send_feature_flags: Either bool or SendFeatureFlagsOptions dict

        Returns:
            SendFeatureFlagsOptions: Normalized options with keys: should_send, only_evaluate_locally,
                  person_properties, group_properties, flag_keys_filter

        Raises:
            TypeError: If send_feature_flags is not bool or dict
        """
        if isinstance(send_feature_flags, dict):
            return {
                "should_send": True,
                "only_evaluate_locally": send_feature_flags.get(
                    "only_evaluate_locally"
                ),
                "person_properties": send_feature_flags.get("person_properties"),
                "group_properties": send_feature_flags.get("group_properties"),
                "flag_keys_filter": send_feature_flags.get("flag_keys_filter"),
            }
        elif isinstance(send_feature_flags, bool):
            return {
                "should_send": send_feature_flags,
                "only_evaluate_locally": None,
                "person_properties": None,
                "group_properties": None,
                "flag_keys_filter": None,
            }
        else:
            raise TypeError(
                f"Invalid type for send_feature_flags: {type(send_feature_flags)}. "
                f"Expected bool or dict."
            )

    @no_throw()
    def set(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        """
        Set properties on a person profile.

        Args:
            distinct_id: The distinct ID of the user.
            properties: A dictionary of properties to set.
            timestamp: The timestamp of the event. UTC is preferred; non-UTC
                datetimes and parseable ISO timestamp strings are converted to UTC.
            uuid: A unique identifier for the event. If provided, it must be a
                valid UUID string or uuid.UUID instance; invalid values are
                ignored and replaced with a newly generated UUID.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            # Set with distinct id
            posthog.set(distinct_id='user123', properties={'name': 'Max Hedgehog'})
            ```

        Category:
            Identification

        Note: This method will not raise exceptions. Errors are logged.
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        timestamp = kwargs.get("timestamp", None)
        uuid = kwargs.get("uuid", None)
        disable_geoip = kwargs.get("disable_geoip", None)

        properties = properties or {}

        properties = add_context_tags(properties)

        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless or not properties:
            return None  # Personless set() does nothing

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set": properties,
            "event": "$set",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    @no_throw()
    def set_once(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        """
        Set properties on a person profile only if they haven't been set before.

        Args:
            distinct_id: The distinct ID of the user.
            properties: A dictionary of properties to set once.
            timestamp: The timestamp of the event. UTC is preferred; non-UTC
                datetimes and parseable ISO timestamp strings are converted to UTC.
            uuid: A unique identifier for the event. If provided, it must be a
                valid UUID string or uuid.UUID instance; invalid values are
                ignored and replaced with a newly generated UUID.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            posthog.set_once(distinct_id='user123', properties={'initial_signup_date': '2024-01-01'})
            ```

        Category:
            Identification

        Note: This method will not raise exceptions. Errors are logged.
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        timestamp = kwargs.get("timestamp", None)
        uuid = kwargs.get("uuid", None)
        disable_geoip = kwargs.get("disable_geoip", None)
        properties = properties or {}

        properties = add_context_tags(properties)

        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless or not properties:
            return None  # Personless set_once() does nothing

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set_once": properties,
            "event": "$set_once",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    @no_throw()
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
        """
        Identify a group and set its properties.

        Args:
            group_type: The type of group (e.g., 'company', 'team'). Required -
                the call is dropped with a warning if it is missing or empty.
            group_key: The unique identifier for the group. Required - the call
                is dropped with a warning if it is missing or empty.
            properties: A dictionary of properties to set on the group.
            timestamp: The timestamp of the event. UTC is preferred; non-UTC
                datetimes and parseable ISO timestamp strings are converted to UTC.
            uuid: A unique identifier for the event. If provided, it must be a
                valid UUID string or uuid.UUID instance; invalid values are
                ignored and replaced with a newly generated UUID.
            disable_geoip: Whether to disable GeoIP for this event.
            distinct_id: The distinct ID of the user performing the action.

        Examples:
            ```python
            posthog.group_identify('company', 'company_id_in_your_db', {
                'name': 'Awesome Inc.',
                'employees': 11
            })
            ```

        Category:
            Identification

        Note: This method will not raise exceptions. Errors are logged.
        """
        if not stringify_id(group_type):
            self.log.warning(
                "group_identify() called without a group_type, dropping the $groupidentify event"
            )
            return None

        if not stringify_id(group_key):
            self.log.warning(
                "group_identify() called without a group_key, dropping the $groupidentify event"
            )
            return None

        properties = properties or {}

        # group_identify is purposefully always personful
        distinct_id = get_identity_state(distinct_id)[0]

        msg: Dict[str, Any] = {
            "event": "$groupidentify",
            "properties": {
                "$group_type": group_type,
                "$group_key": group_key,
                "$group_set": properties,
            },
            "distinct_id": distinct_id,
            "timestamp": timestamp,
            "uuid": uuid,
        }

        # NOTE - group_identify doesn't generally use context properties - should it?
        if get_context_session_id():
            msg["properties"]["$session_id"] = str(get_context_session_id())

        return self._enqueue(msg, disable_geoip)

    @no_throw()
    def alias(
        self,
        previous_id: ID_TYPES,
        distinct_id: Optional[str],
        timestamp: Optional[Union[datetime, str]] = None,
        uuid: Optional[str] = None,
        disable_geoip: Optional[bool] = None,
    ) -> Optional[str]:
        """
        Create an alias between two distinct IDs.

        Args:
            previous_id: The previous distinct ID. Required - the call is dropped
                with a warning if it is missing or empty.
            distinct_id: The new distinct ID to alias to. Falls back to the
                context distinct ID; the call is dropped with a warning if
                neither is available.
            timestamp: The timestamp of the event. UTC is preferred; non-UTC
                datetimes and parseable ISO timestamp strings are converted to UTC.
            uuid: A unique identifier for the event. If provided, it must be a
                valid UUID string or uuid.UUID instance; invalid values are
                ignored and replaced with a newly generated UUID.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            posthog.alias(previous_id='distinct_id', distinct_id='alias_id')
            ```

        Category:
            Identification

        Note: This method will not raise exceptions. Errors are logged.
        """
        previous_id = stringify_id(previous_id)
        if not previous_id:
            self.log.warning(
                "alias() called without a previous_id, dropping the $create_alias event"
            )
            return None

        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless:
            # No alias target was passed and none is available from context.
            self.log.warning(
                "alias() called without a distinct_id, dropping the $create_alias event"
            )
            return None

        msg: Dict[str, Any] = {
            "properties": {
                "distinct_id": previous_id,
                "alias": distinct_id,
            },
            "timestamp": timestamp,
            "event": "$create_alias",
            "distinct_id": previous_id,
            "uuid": uuid,
        }

        if get_context_session_id():
            msg["properties"]["$session_id"] = str(get_context_session_id())

        return self._enqueue(msg, disable_geoip)

    def capture_exception(
        self,
        exception: Optional[ExceptionArg],
        **kwargs: Unpack[OptionalCaptureArgs],
    ) -> Optional[str]:
        """
        Capture an exception for error tracking.

        When OpenTelemetry is installed and a valid span is active, its trace and
        span IDs are added as ``$trace_id`` and ``$span_id`` event properties.

        Args:
            exception: The exception to capture.
            distinct_id: The distinct ID of the user.
            properties: A dictionary of additional properties.
            flags: A ``FeatureFlagEvaluations`` snapshot from ``evaluate_flags()``.
                Attaches those exact flag values to the captured `$exception` event.
            send_feature_flags: Deprecated. Pass ``flags`` from ``evaluate_flags()`` instead.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            try:
                # Some code that might fail
                pass
            except Exception as e:
                posthog.capture_exception(e, 'user_distinct_id', properties=additional_properties)
            ```

        Category:
            Error Tracking
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        flags_snapshot = kwargs.get("flags", None)
        send_feature_flags = kwargs.get("send_feature_flags", False)
        disable_geoip = kwargs.get("disable_geoip", None)
        # this function shouldn't ever throw an error, so it logs exceptions instead of raising them.
        # this is important to ensure we don't unexpectedly re-raise exceptions in the user's code.
        try:
            properties = properties or {}

            # Check if this exception has already been captured
            if exception is not None and exception_is_already_captured(exception):
                self.log.debug("Exception already captured, skipping")
                return None

            if exception is not None:
                exc_info = exc_info_from_error(exception)
            else:
                exc_info = sys.exc_info()

            if exc_info is None or exc_info == (None, None, None):
                self.log.warning("No exception information available")
                return None

            # Format stack trace for cymbal
            all_exceptions_with_trace = exceptions_from_error_tuple(exc_info)

            # Add in-app property to frames in the exceptions
            event = handle_in_app(
                {
                    "exception": {
                        "values": all_exceptions_with_trace,
                    },
                },
                in_app_include=self.in_app_modules,
                project_root=self.project_root,
            )
            all_exceptions_with_trace_and_in_app = event["exception"]["values"]

            properties = {
                "$exception_list": all_exceptions_with_trace_and_in_app,
                **_get_current_otel_span_properties(),
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

            timestamp = kwargs.get("timestamp", None)
            uuid = kwargs.get("uuid", None)
            groups = kwargs.get("groups", None)
            res = self.capture(
                "$exception",
                distinct_id=distinct_id,
                properties=properties,
                timestamp=timestamp,
                uuid=uuid,
                groups=groups,
                flags=flags_snapshot,
                send_feature_flags=send_feature_flags,
                disable_geoip=disable_geoip,
            )

            # Mark the exception as captured to prevent duplicate captures
            if exception is not None and res is not None:
                mark_exception_as_captured(exception, res)

            return res
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")
            return None

    @classmethod
    def _reinit_client_registry_after_fork(cls):
        """Replace the inherited registry lock once in each forked child."""
        child_pid = os.getpid()
        if cls._client_registry_pid == child_pid:
            return

        # The lock may have been held by a parent thread at fork time. Replace it
        # without acquiring it, while preserving inherited active-client records.
        cls._client_registry_lock = threading.Lock()
        cls._client_registry_pid = child_pid

    @staticmethod
    def _reinit_after_fork_weak(weak_self):
        """
        Reinitialize the client after a fork.
        Garbage collected if the client is deleted.
        """
        Client._reinit_client_registry_after_fork()

        self = weak_self()
        if self is None:
            return
        self._reinit_after_fork()

    def _reinit_after_fork(self):
        """Reinitialize fork-unsafe client state in a forked child process.

        Registered via os.register_at_fork(after_in_child=...) so it runs
        exactly once in each child, before any user code, covering all code
        paths (capture, flush, join, etc.).

        Python threads do not survive fork(), so each lane's queue and
        consumer pool are rebuilt (see `_Lane.rebuild_after_fork`).
        """
        terminal_requested = (
            self._join_requested or self._shutdown_requested or self._workers_joined
        )
        for lane in self._lanes:
            lane.rebuild_after_fork(closed=terminal_requested)

        self._lifecycle_lock = threading.Lock()
        self._lifecycle_condition = threading.Condition(self._lifecycle_lock)
        self._lifecycle_owner = None
        self._deferred_lifecycle_thread_pending = False
        self._deferred_lifecycle_dirty = False
        self._lifecycle_callback_context = ContextVar(
            "posthog_lifecycle_callback", default=False
        )
        self._deferred_flush_lock = threading.Lock()
        self._deferred_flush_pending = False
        self._deferred_flush_followup = False
        self._deferred_flush_followup_timeout = None

        # Async runner threads do not survive fork(); recreate lazily on next async cache call.
        self._flag_definition_cache_provider_async_runner = None
        self._flag_definition_cache_provider_async_runner_lock = threading.Lock()

        # A parent thread may have been publishing or caching flag definitions at
        # fork time.
        self._flag_definition_publication_lock = threading.Lock()
        self._flag_definition_cache_write_lock = threading.RLock()

        # Metrics locks may have been held by a parent thread at fork time; replace
        # them (never acquire them) so the child can't deadlock on a vanished holder.
        self._metrics_lock = threading.Lock()
        if self._metrics is not None:
            self._metrics._reinit_after_fork()

        # If using Redis cache, we must reinitialize to get a fresh connection (fork-safe).
        # If using Memory cache, we keep it as-is to benefit from the inherited warm cache.
        if isinstance(self.flag_cache, RedisFlagCache):
            self.flag_cache = self._initialize_flag_cache(self.flag_fallback_cache_url)

        reset_sessions()

        # Start child threads only after replacing every lock they can touch.
        if terminal_requested:
            self.poller = None
        elif self.enable_local_evaluation:
            self.poller = Poller(
                interval=timedelta(seconds=self.poll_interval),
                execute=self._load_feature_flags,
            )
            self.poller.start()
        else:
            self.poller = None

    def _normalize_event_uuid(self, msg):
        # type: (...) -> None
        """Ensure `msg["uuid"]` is a valid uuid string, generating one if missing or invalid."""
        if "uuid" in msg:
            uuid = msg.pop("uuid")
            if uuid is not None:
                try:
                    msg["uuid"] = _stringify_event_uuid(uuid)
                except ValueError as e:
                    self.log.error("%s Falling back to a generated UUID.", e)

        if "uuid" not in msg:
            # Always send a uuid, so we can always return one
            msg["uuid"] = stringify_id(uuid4())

    def _enqueue(self, msg, disable_geoip, lane=None, property_allowlist=None):
        # type: (...) -> Optional[str]
        """Push a new `msg` onto a lane's queue (analytics when unspecified), return the event uuid or None."""

        if lane is None:
            lane = self._analytics_lane

        if self.disabled:
            return None

        timestamp = msg["timestamp"]
        if timestamp is None:
            timestamp = datetime.now(tz=timezone.utc)

        # add common
        try:
            msg["timestamp"] = _normalize_timestamp(timestamp)
        except ValueError:
            self.log.warning(
                "Invalid timestamp %r. Falling back to the current UTC time.", timestamp
            )
            msg["timestamp"] = datetime.now(tz=timezone.utc).isoformat()

        self._normalize_event_uuid(msg)

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

        # Set after the super_properties merge so this SDK's server classification
        # can't be silently overridden by a user-provided super property.
        if self.is_server:
            msg["properties"]["$is_server"] = True

        # Applied after every enrichment step (system context, context tags, super
        # properties, $lib/$lib_version) so the final event shape is exactly the
        # allowlist regardless of where a property came from.
        if property_allowlist is not None:
            msg["properties"] = {
                k: v for k, v in msg["properties"].items() if k in property_allowlist
            }

        msg["distinct_id"] = stringify_id(msg.get("distinct_id", None))

        msg = clean(msg)

        if self.before_send:
            try:
                modified_msg = self.before_send(msg)
                if modified_msg is None:
                    self.log.debug("Event dropped by before_send callback")
                    return None
                if not isinstance(modified_msg, dict):
                    raise TypeError("before_send must return a dict or None")
                msg = clean(modified_msg)
            except Exception as e:
                self.log.exception(f"Error in before_send callback: {e}")
                return None

        # Re-normalized after before_send, which may have replaced or removed
        # msg["uuid"], so the returned uuid always matches the wire event.
        self._normalize_event_uuid(msg)
        sent_uuid = msg["uuid"]

        self.log.debug("queueing: %s", msg)

        # if send is False, return msg as if it was successfully queued, unless
        # shutdown has already closed this lane's admission.
        if not self.send:
            if lane.run_sync_if_open(lambda: None):
                return sent_uuid
            self.log.warning(
                "%s lane received event %s after shutdown, dropping it",
                lane.name,
                msg["event"],
            )
            return None

        if self.sync_mode:
            self.log.debug("enqueued with blocking %s.", msg["event"])

            def send_sync() -> None:
                # Sync mode bypasses the lane's queue but keeps its wire config:
                # the AI lane is pinned to v0, so its events post to the AI
                # endpoint regardless of `capture_mode`.
                if lane.capture_mode == CaptureMode.V1:
                    _send_v1_batch(
                        self.api_key,
                        self.host,
                        [msg],
                        compression=self.capture_compression,
                        timeout=self.timeout,
                        max_retries=self.max_retries,
                        historical_migration=self.historical_migration,
                    )
                    return

                batch_post(
                    self.api_key,
                    self.host,
                    gzip=self.gzip,
                    timeout=self.timeout,
                    batch=[msg],
                    historical_migration=self.historical_migration,
                    path=lane.endpoint,
                )

            if lane.run_sync_if_open(send_sync):
                return sent_uuid
            self.log.warning(
                "%s lane received event %s after shutdown, dropping it",
                lane.name,
                msg["event"],
            )
            return None

        if lane.enqueue(msg):
            self.log.debug("enqueued %s.", msg["event"])
            return sent_uuid

        if not lane.available:
            self.log.warning(
                "%s lane is unavailable because a compatible queue could not be "
                "initialized, dropping event %s",
                lane.name,
                msg["event"],
            )
        elif lane._closed:
            self.log.warning(
                "%s lane received event %s after shutdown, dropping it",
                lane.name,
                msg["event"],
            )
        else:
            self.log.warning(
                "%s lane queue is full (maxsize %d), dropping event %s",
                lane.name,
                lane.queue.maxsize,
                msg["event"],
            )
        return None

    @property
    def metrics(self) -> PostHogMetrics:
        """
        The `posthog.metrics` API: a statsd-style pre-aggregating metrics client — alpha.

        Samples fold into per-series aggregates in memory and flush as one OTLP
        data point per series per window, so recording from hot paths is cheap.
        Configure via the ``metrics`` client option; pending metrics flush on
        ``shutdown()``.

        Examples:
            ```python
            client = Posthog("<ph_project_api_key>", metrics={"service_name": "billing-worker"})
            client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
            client.metrics.gauge("queue.depth", 42)
            client.metrics.histogram("job.duration", 187, unit="ms")
            ```
        """
        if self._metrics is None:
            with self._metrics_lock:
                if self._metrics is None:
                    # Same no-throw semantics as the rest of the public client surface:
                    # a bad metrics config degrades to defaults instead of raising from
                    # the first chained metrics.count() call (raise only in debug mode).
                    try:
                        self._metrics = PostHogMetrics(self, self._metrics_config)
                    except Exception as e:
                        if self.debug:
                            raise e
                        self.log.exception(
                            f"Error initializing metrics, using default configuration: {e}"
                        )
                        self._metrics = PostHogMetrics(self, None)
        return self._metrics

    def flush(self, timeout_seconds: Optional[float] = 10) -> None:
        """
        Force a flush from the internal queue to the server. Do not use directly, call `shutdown()` instead.

        Args:
            timeout_seconds: Maximum seconds to wait for the queue to flush.
                Defaults to 10 seconds. Pass ``None`` to wait indefinitely.

        Examples:
            ```python
            posthog.capture('event_name')
            posthog.flush()  # Ensures the event is sent immediately
            ```
        """
        if self._defer_flush_from_callback(timeout_seconds):
            return
        try:
            if timeout_seconds is None:
                for lane in self._lanes:
                    lane.flush(None)
                return

            # The timeout is a total budget shared by the lanes, so flush()
            # returns within roughly `timeout_seconds` overall.
            deadline = time.monotonic() + timeout_seconds
            for lane in self._lanes:
                lane.flush(max(0.0, deadline - time.monotonic()))
        except Exception as e:
            self.log.exception("error flushing queue: %s", e)
            return

    def _is_consumer_thread(self) -> bool:
        current = threading.current_thread()
        return any(current in lane.consumers for lane in self._lanes)

    def _is_lifecycle_callback_thread(self) -> bool:
        if self._lifecycle_callback_context.get():
            return True
        current = threading.current_thread()
        if self._is_consumer_thread() or current is self.poller:
            return True
        runner = self._flag_definition_cache_provider_async_runner
        return runner is not None and runner.owns_thread(current)

    def _start_lifecycle_thread(self, target, name: str, *args) -> None:
        threading.Thread(
            target=target,
            args=args,
            name=f"posthog-{name}",
            daemon=True,
        ).start()

    def _defer_lifecycle_from_callback(self) -> bool:
        if not self._is_lifecycle_callback_thread():
            return False
        with self._lifecycle_lock:
            self._deferred_lifecycle_dirty = True
            if self._deferred_lifecycle_thread_pending:
                return True
            self._deferred_lifecycle_thread_pending = True

        def run() -> None:
            while True:
                with self._lifecycle_lock:
                    self._deferred_lifecycle_dirty = False
                    require_shutdown = self._shutdown_requested
                operation = "shutdown" if require_shutdown else "join"
                try:
                    self._run_lifecycle(require_shutdown=require_shutdown)
                except BaseException:
                    self.log.exception("Deferred %s failed", operation)

                with self._lifecycle_lock:
                    if self._deferred_lifecycle_dirty:
                        continue
                    self._deferred_lifecycle_thread_pending = False
                    return

        self._start_lifecycle_thread(run, "lifecycle")
        return True

    def _defer_flush_from_callback(self, timeout_seconds: Optional[float]) -> bool:
        if not self._is_lifecycle_callback_thread():
            return False
        with self._deferred_flush_lock:
            if self._deferred_flush_pending:
                if not self._deferred_flush_followup:
                    self._deferred_flush_followup = True
                    self._deferred_flush_followup_timeout = timeout_seconds
                elif (
                    self._deferred_flush_followup_timeout is not None
                    and timeout_seconds is not None
                ):
                    self._deferred_flush_followup_timeout = max(
                        self._deferred_flush_followup_timeout, timeout_seconds
                    )
                else:
                    self._deferred_flush_followup_timeout = None
                return True
            self._deferred_flush_pending = True

        def run() -> None:
            next_timeout = timeout_seconds
            while True:
                self.flush(next_timeout)
                with self._deferred_flush_lock:
                    if self._deferred_flush_followup:
                        next_timeout = self._deferred_flush_followup_timeout
                        self._deferred_flush_followup = False
                        self._deferred_flush_followup_timeout = None
                        continue
                    self._deferred_flush_pending = False
                    return

        self._start_lifecycle_thread(run, "flush")
        return True

    def _run_lifecycle_cleanup(
        self,
        log_message: str,
        cleanup: Callable[[], None],
        errors: list[Exception],
    ) -> None:
        """Attempt one cleanup step without preventing later independent steps."""
        try:
            cleanup()
        except Exception as error:
            self.log.exception(log_message)
            errors.append(error)

    def _flush_or_discard_queues(self, errors: list[Exception]) -> None:
        for lane in self._lanes:
            try:
                if any(consumer.is_alive() for consumer in lane.consumers):
                    lane.flush(timeout_seconds=None)
                else:
                    lane.discard_undrainable_queued_work()
            except Exception as error:
                self.log.exception(
                    "Failed to drain %s lane during lifecycle cleanup", lane.name
                )
                errors.append(error)

    def _join_once(
        self,
        errors: list[Exception],
        flush_queues: bool = True,
        *,
        lanes_prepared: bool = False,
    ) -> None:
        if not self._workers_joined:
            if not lanes_prepared:
                for lane in self._lanes:
                    self._run_lifecycle_cleanup(
                        f"Failed to close {lane.name} lane during lifecycle cleanup",
                        lane.close,
                        errors,
                    )
                for lane in self._lanes:
                    self._run_lifecycle_cleanup(
                        f"Failed waiting for {lane.name} synchronous sends during lifecycle cleanup",
                        lane.wait_for_sync_sends,
                        errors,
                    )
            if flush_queues:
                self._flush_or_discard_queues(errors)
            for lane in self._lanes:
                self._run_lifecycle_cleanup(
                    f"Failed to stop {lane.name} lane during lifecycle cleanup",
                    lane.join,
                    errors,
                )
            # Ordinary cleanup failures are logged by each step. Reaching here
            # means every worker cleanup step was attempted once.
            self._workers_joined = True

        if not self._join_cleanup_complete:
            if self.poller:
                self._run_lifecycle_cleanup(
                    "Failed to stop feature flag poller during lifecycle cleanup",
                    self.poller.stop,
                    errors,
                )

            self._run_lifecycle_cleanup(
                "Failed to shut down feature flag cache provider during lifecycle cleanup",
                self._shutdown_flag_definition_cache_provider,
                errors,
            )
            self._run_lifecycle_cleanup(
                "Failed to unregister client during lifecycle cleanup",
                self._unregister_duplicate_client,
                errors,
            )
            self._join_cleanup_complete = True

    def _shutdown_once(self, errors: list[Exception]) -> None:
        if not self._workers_joined:
            # Close every lane before draining any of them so no producer can be
            # admitted between a completed flush and consumer shutdown.
            for lane in self._lanes:
                self._run_lifecycle_cleanup(
                    f"Failed to close {lane.name} lane during shutdown",
                    lane.close,
                    errors,
                )
            for lane in self._lanes:
                self._run_lifecycle_cleanup(
                    f"Failed waiting for {lane.name} synchronous sends during shutdown",
                    lane.wait_for_sync_sends,
                    errors,
                )
            self._flush_or_discard_queues(errors)

        if self._metrics is not None:
            self._run_lifecycle_cleanup(
                "Failed to flush metrics on shutdown", self._metrics.flush, errors
            )
            self._run_lifecycle_cleanup(
                "Failed to reset metrics on shutdown", self._metrics.reset, errors
            )
        self._join_once(errors, flush_queues=False, lanes_prepared=True)
        self._run_lifecycle_cleanup(
            "Failed to clear feature flag deduplication state on shutdown",
            self.distinct_ids_feature_flags_reported.clear,
            errors,
        )

        if self.exception_capture:
            self._run_lifecycle_cleanup(
                "Failed to close exception capture on shutdown",
                self.exception_capture.close,
                errors,
            )
        # Ordinary cleanup failures are logged by each step. Reaching here
        # means every shutdown cleanup step was attempted once.
        self._shutdown_complete = True

    def _run_lifecycle(self, require_shutdown: bool = False) -> None:
        while True:
            with self._lifecycle_condition:
                if require_shutdown and self._shutdown_complete:
                    if self.debug and self._lifecycle_cleanup_failed:
                        raise RuntimeError("client lifecycle cleanup failed")
                    return
                if not require_shutdown and (
                    self._join_cleanup_complete or self._shutdown_complete
                ):
                    if self.debug and self._lifecycle_cleanup_failed:
                        raise RuntimeError("client lifecycle cleanup failed")
                    return
                if self._lifecycle_owner is not None:
                    if self._is_lifecycle_callback_thread() or (
                        threading.current_thread() is self._lifecycle_owner
                    ):
                        return
                    self._lifecycle_condition.wait()
                    continue
                self._lifecycle_owner = threading.current_thread()

            try:
                errors: list[Exception] = []
                while True:
                    with self._lifecycle_lock:
                        run_shutdown = self._shutdown_requested

                    if run_shutdown:
                        self._shutdown_once(errors)
                    else:
                        self._join_once(errors)

                    with self._lifecycle_condition:
                        if (
                            self._shutdown_requested
                            and not self._shutdown_complete
                            and not run_shutdown
                        ):
                            continue
                        if errors:
                            self._lifecycle_cleanup_failed = True
                            raise errors[0]
                        self._lifecycle_owner = None
                        self._lifecycle_condition.notify_all()
                        return
            except BaseException:
                with self._lifecycle_condition:
                    self._lifecycle_owner = None
                    self._lifecycle_condition.notify_all()
                raise

    @no_throw()
    def _atexit(self) -> None:
        """Make a bounded delivery attempt, then stop daemon workers."""
        with self._lifecycle_condition:
            # A daemon lifecycle worker already owns cleanup. Do not wait for it
            # at interpreter exit; the process must remain free to terminate.
            if self._lifecycle_owner is not None:
                return
            self._lifecycle_owner = threading.current_thread()

        try:
            try:
                for lane in self._lanes:
                    lane.close()

                deadline = _get_atexit_deadline()
                for lane in self._lanes:
                    lane.flush(max(0.0, deadline - time.monotonic()))
            finally:
                # Consumers are daemon threads. Publish a non-draining stop to
                # every consumer, but do not join in-flight requests at exit.
                for lane in self._lanes:
                    for consumer in lane.consumers:
                        consumer.pause()
        finally:
            with self._lifecycle_condition:
                self._lifecycle_owner = None
                self._lifecycle_condition.notify_all()

    @no_throw()
    def join(self) -> None:
        """
        Attempt to process queued events and end the consumer threads. Do not use directly, call `shutdown()` instead.

        Failed or undrainable events may be dropped and reported through logging
        or ``on_error``; returning does not guarantee server receipt. Lifecycle
        cleanup is attempted once, and cleanup failures are logged without retry.

        Examples:
            ```python
            posthog.join()
            ```
        """
        with self._lifecycle_lock:
            self._join_requested = True
        if self._defer_lifecycle_from_callback():
            return
        self._run_lifecycle()

    @no_throw()
    def shutdown(self) -> None:
        """
        Flush all messages and cleanly shutdown the client. Call this before the process ends in serverless environments to avoid data loss.

        Normally this method blocks until queued events have been attempted and
        cleanup finishes. Failed or undrainable events may be dropped and
        reported through logging or ``on_error``; returning does not guarantee
        server receipt. Lifecycle cleanup is attempted once, and cleanup failures
        are logged without retry. When called directly from an SDK callback such as
        ``on_error``, shutdown is deferred to avoid blocking the worker that
        invoked the callback. If the callback must coordinate a blocking
        shutdown, have it signal an
        application-owned thread and return before that thread calls shutdown.
        Do not wait inside the callback for another thread or task that calls a
        lifecycle method.

        Examples:
            ```python
            posthog.shutdown()
            ```
        """
        with self._lifecycle_lock:
            if self._shutdown_complete:
                if self.debug and self._lifecycle_cleanup_failed:
                    raise RuntimeError("client lifecycle cleanup failed")
                return
            self._shutdown_requested = True
        if self._defer_lifecycle_from_callback():
            return
        self._run_lifecycle(require_shutdown=True)

    def _resolve_flag_definition_cache_provider_result(self, result):
        if not inspect.isawaitable(result):
            return result

        with self._flag_definition_cache_provider_async_runner_lock:
            if self._flag_definition_cache_provider_async_runner is None:
                self._flag_definition_cache_provider_async_runner = (
                    _BackgroundEventLoopRunner()
                )
            token = self._lifecycle_callback_context.set(True)
            try:
                return self._flag_definition_cache_provider_async_runner.run(result)
            finally:
                self._lifecycle_callback_context.reset(token)

    def _shutdown_flag_definition_cache_provider(self):
        if not self._flag_definition_cache_provider:
            return

        try:
            self._resolve_flag_definition_cache_provider_result(
                self._flag_definition_cache_provider.shutdown()
            )
        except Exception as e:
            self.log.error(f"[FEATURE FLAGS] Cache provider shutdown error: {e}")
        finally:
            with self._flag_definition_cache_provider_async_runner_lock:
                if self._flag_definition_cache_provider_async_runner:
                    self._flag_definition_cache_provider_async_runner.close()
                    self._flag_definition_cache_provider_async_runner = None

    def _update_flag_state(
        self, data: FlagDefinitionCacheData, old_flags_by_key: Optional[dict] = None
    ) -> None:
        """Update internal flag state from cache data and invalidate evaluation cache if changed."""
        self.feature_flags = data["flags"]
        self.group_type_mapping = data["group_type_mapping"]
        self.cohorts = data["cohorts"]
        # Server-controlled gate for minimal $feature_flag_called events; the
        # local-evaluation payload carries it as a top-level key. Absent means False.
        self._minimal_flag_called_events = (
            data.get("minimal_flag_called_events") is True
        )

        # Invalidate evaluation cache if flag definitions changed
        if (
            self.flag_cache
            and old_flags_by_key is not None
            and old_flags_by_key != (self.feature_flags_by_key or {})
        ):
            old_version = self.flag_definition_version
            self.flag_definition_version += 1
            self.flag_cache.invalidate_version(old_version)

    def _load_feature_flags(self):
        should_fetch = True
        if self._flag_definition_cache_provider:
            try:
                should_fetch = self._resolve_flag_definition_cache_provider_result(
                    self._flag_definition_cache_provider.should_fetch_flag_definitions()
                )
            except Exception as e:
                self.log.error(
                    f"[FEATURE FLAGS] Cache provider should_fetch error: {e}"
                )
                # Fail-safe: fetch from API if cache provider errors
                should_fetch = True

        # If not fetching, try to get from cache
        if not should_fetch and self._flag_definition_cache_provider:
            try:
                cached_data = self._resolve_flag_definition_cache_provider_result(
                    self._flag_definition_cache_provider.get_flag_definitions()
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
                else:
                    # Emergency fallback: if cache is empty and we have no flags, fetch anyway.
                    # There's really no other way of recovering in this case.
                    if not self.feature_flags:
                        self.log.debug(
                            "[FEATURE FLAGS] Cache empty and no flags loaded, falling back to API fetch"
                        )
                        should_fetch = True
            except Exception as e:
                self.log.error(f"[FEATURE FLAGS] Cache provider get error: {e}")
                # Fail-safe: fetch from API if cache provider errors
                should_fetch = True

        if should_fetch:
            self._fetch_feature_flags_from_api()

    def _fetch_feature_flags_from_api(self):
        """Fetch feature flags from the PostHog API."""
        personal_api_key = self.personal_api_key
        if personal_api_key is None:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a secret_key to use feature flags."
            )
            return

        with self._flag_definition_publication_lock:
            self._flag_definition_fetch_generation += 1
            fetch_generation = self._flag_definition_fetch_generation
            request_etag = self._flags_etag

        cache_data_to_store: Optional[FlagDefinitionCacheData] = None
        try:
            response = get(
                personal_api_key,
                f"/flags/definitions?token={self.api_key}&send_cohorts",
                self.host,
                timeout=10,
                etag=request_etag,
            )

            with self._flag_definition_publication_lock:
                if fetch_generation <= self._flag_definition_published_generation:
                    self.log.debug(
                        "[FEATURE FLAGS] Ignoring stale flag definition response"
                    )
                    self._last_feature_flag_poll = datetime.now(tz=timezone.utc)
                    return

                # A 304 is valid only for the ETag used by this request. Another
                # overlapping response may already have installed newer definitions.
                if response.not_modified:
                    if self._flags_etag != request_etag:
                        self.log.debug(
                            "[FEATURE FLAGS] Ignoring stale 304 flag definition response"
                        )
                        self._last_feature_flag_poll = datetime.now(tz=timezone.utc)
                        return

                    self._flags_etag = response.etag
                    self._flag_definition_published_generation = fetch_generation
                    self._flag_definition_cache_generation = fetch_generation
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

                old_flags_by_key: dict[str, dict] = self.feature_flags_by_key or {}
                self._update_flag_state(
                    response.data, old_flags_by_key=old_flags_by_key
                )

                if self._flag_definition_cache_provider:
                    cache_data_to_store = {
                        "flags": self.feature_flags or [],
                        "group_type_mapping": self.group_type_mapping or {},
                        "cohorts": self.cohorts or {},
                        "minimal_flag_called_events": self._minimal_flag_called_events,
                    }

                # Publish the ETag only after its matching flag state is installed.
                self._flags_etag = response.etag
                self._flag_definition_published_generation = fetch_generation
                self._flag_definition_cache_generation = fetch_generation

            if cache_data_to_store and self._flag_definition_cache_provider:
                # Keep provider I/O out of the publication lock. The separate lock
                # preserves cache write order without delaying newer API fetches or
                # in-memory publication.
                with self._flag_definition_cache_write_lock:
                    with self._flag_definition_publication_lock:
                        should_store = (
                            fetch_generation == self._flag_definition_cache_generation
                        )
                    if should_store:
                        try:
                            self._resolve_flag_definition_cache_provider_result(
                                self._flag_definition_cache_provider.on_flag_definitions_received(
                                    cache_data_to_store
                                )
                            )
                        except Exception as e:
                            self.log.error(
                                f"[FEATURE FLAGS] Cache provider store error: {e}"
                            )
                            # Flags are already in memory, so continue normally

        except APIError as e:
            with self._flag_definition_publication_lock:
                if fetch_generation <= self._flag_definition_published_generation:
                    self.log.debug("[FEATURE FLAGS] Ignoring stale API error response")
                elif e.status == 401:
                    detail = (
                        f"Error loading feature flags: {e.message}. "
                        "Please verify both your project_api_key and secret_key. "
                        "More information: https://posthog.com/docs/api/overview"
                    )
                    self.log.error("[FEATURE FLAGS] %s", detail)
                    self.feature_flags = []
                    self.group_type_mapping = {}
                    self.cohorts = {}
                    self._flags_etag = None
                    self._flag_definition_published_generation = fetch_generation
                    self._flag_definition_cache_generation = fetch_generation

                    if self.flag_cache:
                        self.flag_cache.clear()

                    if self.debug:
                        raise APIError(status=401, message=detail)
                elif e.status == 402:
                    self.log.warning(
                        "[FEATURE FLAGS] PostHog feature flags quota limited, resetting feature flag data.  Learn more about billing limits at https://posthog.com/docs/billing/limits-alerts"
                    )
                    # Reset all feature flag data when quota limited
                    self.feature_flags = []
                    self.group_type_mapping = {}
                    self.cohorts = {}
                    self._flags_etag = None
                    self._flag_definition_published_generation = fetch_generation
                    self._flag_definition_cache_generation = fetch_generation

                    # Clear flag cache when quota limited
                    if self.flag_cache:
                        self.flag_cache.clear()

                    if self.debug:
                        raise APIError(
                            status=402,
                            message="PostHog feature flags quota limited",
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

    def load_feature_flags(self):
        """
        Load feature flags for local evaluation.

        Examples:
            ```python
            posthog.load_feature_flags()
            ```

        Category:
            Feature flags
        """
        if self.disabled:
            self.feature_flags = []
            return

        if not self.personal_api_key:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a secret_key to use feature flags."
            )
            self.feature_flags = []
            return

        self._load_feature_flags()

        # Only start the poller if local evaluation is enabled
        if self.enable_local_evaluation and not (
            self.poller and self.poller.is_alive()
        ):
            self.poller = Poller(
                interval=timedelta(seconds=self.poll_interval),
                execute=self._load_feature_flags,
            )
            self.poller.start()

    def _compute_flag_locally(
        self,
        feature_flag,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        warn_on_unknown_groups=True,
        device_id=None,
    ) -> FlagValue:
        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        # Create evaluation cache for flag dependencies
        evaluation_cache: dict[str, Optional[FlagValue]] = {}

        if feature_flag.get("ensure_experience_continuity", False):
            raise InconclusiveMatchError("Flag has experience continuity enabled")

        if not feature_flag.get("active"):
            return False

        flag_filters = feature_flag.get("filters") or {}
        aggregation_group_type_index = flag_filters.get("aggregation_group_type_index")
        group_type_mapping = self.group_type_mapping or {}

        if aggregation_group_type_index is not None:
            group_name = group_type_mapping.get(str(aggregation_group_type_index))

            if not group_name:
                self.log.warning(
                    f"[FEATURE FLAGS] Unknown group type index {aggregation_group_type_index} for feature flag {feature_flag['key']}"
                )
                # failover to `/flags`
                raise InconclusiveMatchError("Flag has unknown group type index")

            if group_name not in groups:
                # Group flags are never enabled in `groups` aren't passed in
                # don't failover to `/flags`, since response will be the same
                if warn_on_unknown_groups:
                    self.log.warning(
                        f"[FEATURE FLAGS] Can't compute group feature flag: {feature_flag['key']} without group names passed in"
                    )
                else:
                    self.log.debug(
                        f"[FEATURE FLAGS] Can't compute group feature flag: {feature_flag['key']} without group names passed in"
                    )
                return False

            if group_name not in group_properties:
                raise InconclusiveMatchError(
                    f"Flag has no group properties for group '{group_name}'"
                )
            focused_group_properties = group_properties[group_name]
            group_key = groups[group_name]
            return match_feature_flag_properties(
                feature_flag,
                group_key,
                focused_group_properties,
                cohort_properties=self.cohorts,
                flags_by_key=self.feature_flags_by_key,
                evaluation_cache=evaluation_cache,
                device_id=device_id,
                bucketing_value=group_key,
                group_type_mapping=group_type_mapping,
                groups=groups,
                group_properties=group_properties,
            )
        else:
            bucketing_value = resolve_bucketing_value(
                feature_flag, distinct_id, device_id
            )
            return match_feature_flag_properties(
                feature_flag,
                distinct_id,
                person_properties,
                cohort_properties=self.cohorts,
                flags_by_key=self.feature_flags_by_key,
                evaluation_cache=evaluation_cache,
                device_id=device_id,
                bucketing_value=bucketing_value,
                group_type_mapping=group_type_mapping,
                groups=groups,
                group_properties=group_properties,
            )

    def feature_enabled(
        self,
        key: str,
        distinct_id: ID_TYPES,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = True,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[bool]:
        """
        Check if a feature flag is enabled for a user.

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.
            device_id: The device ID for this request.

        Examples:
            ```python
            is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')
            if is_my_flag_enabled:
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
            ```

        Category:
            Feature flags
        """
        warnings.warn(
            "`feature_enabled` is deprecated and will be removed in a future major version. "
            "Use `posthog.evaluate_flags(distinct_id, ...)` and call `flags.is_enabled(key)` "
            "instead — this consolidates flag evaluation into a single `/flags` request per "
            "incoming request.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Bypass the public `get_feature_flag` so the user only sees a single deprecation
        # warning per call, not three (feature_enabled → get_feature_flag → get_feature_flag_result).
        flag_result = self._get_feature_flag_result(
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
        response = flag_result.get_value() if flag_result else None

        if response is None:
            return None
        return bool(response)

    def _get_stale_flag_fallback(
        self, distinct_id: ID_TYPES, key: str
    ) -> Optional[FeatureFlagResult]:
        """Returns a stale cached flag value if available, otherwise None."""
        if self.flag_cache:
            stale_result = self.flag_cache.get_stale_cached_flag(distinct_id, key)
            if isinstance(stale_result, FeatureFlagResult):
                self.log.info(
                    f"[FEATURE FLAGS] Using stale cached value for flag {key}"
                )
                return stale_result
        return None

    def _get_feature_flag_result(
        self,
        key: str,
        distinct_id: ID_TYPES,
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

        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                groups or {},
                person_properties or {},
                group_properties or {},
            )
        )
        # Ensure non-None values for type checking
        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        flag_result = None
        flag_details = None
        request_id = None
        evaluated_at = None
        feature_flag_error: Optional[str] = None
        remote_minimal_flag_called_events = False

        # Resolve device_id from context if not provided
        if device_id is None:
            device_id = get_context_device_id()

        local_person_properties = self._person_properties_for_local_evaluation(
            distinct_id, person_properties
        )
        flag_value = self._locally_evaluate_flag(
            key,
            distinct_id,
            groups,
            local_person_properties,
            group_properties,
            device_id,
        )
        flag_was_locally_evaluated = flag_value is not None

        if flag_value is not None:
            lookup_match_value = (
                override_match_value if override_match_value is not None else flag_value
            )
            payload = (
                self._compute_payload_locally(key, lookup_match_value)
                if lookup_match_value is not None
                else None
            )
            flag_result = FeatureFlagResult.from_value_and_payload(
                key, lookup_match_value, payload
            )

            # Cache the local evaluation, not a payload lookup override.
            cached_flag_result = flag_result
            if override_match_value is not None:
                cached_flag_result = FeatureFlagResult.from_value_and_payload(
                    key, flag_value, self._compute_payload_locally(key, flag_value)
                )
            if self.flag_cache and cached_flag_result:
                self.flag_cache.set_cached_flag(
                    distinct_id, key, cached_flag_result, self.flag_definition_version
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
                    remote_minimal_flag_called_events,
                ) = self._get_feature_flag_details_from_server(
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

                # Cache successful remote evaluation
                if self.flag_cache and flag_result:
                    self.flag_cache.set_cached_flag(
                        distinct_id, key, flag_result, self.flag_definition_version
                    )

                self.log.debug(
                    f"Successfully computed flag remotely: #{key} -> #{flag_result}"
                )
            except QuotaLimitError as e:
                self.log.warning(f"[FEATURE FLAGS] Quota limit exceeded: {e}")
                feature_flag_error = FeatureFlagError.QUOTA_LIMITED
                flag_result = self._get_stale_flag_fallback(distinct_id, key)
            except RequestsTimeout as e:
                self.log.warning(f"[FEATURE FLAGS] Request timed out: {e}")
                feature_flag_error = FeatureFlagError.TIMEOUT
                flag_result = self._get_stale_flag_fallback(distinct_id, key)
            except RequestsConnectionError as e:
                self.log.warning(f"[FEATURE FLAGS] Connection error: {e}")
                feature_flag_error = FeatureFlagError.CONNECTION_ERROR
                flag_result = self._get_stale_flag_fallback(distinct_id, key)
            except APIError as e:
                self.log.warning(f"[FEATURE FLAGS] API error: {e}")
                feature_flag_error = FeatureFlagError.api_error(e.status)
                flag_result = self._get_stale_flag_fallback(distinct_id, key)
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get flag remotely: {e}")
                feature_flag_error = FeatureFlagError.UNKNOWN_ERROR
                flag_result = self._get_stale_flag_fallback(distinct_id, key)

        if send_feature_flag_events:
            # Locally-evaluated flags carry has_experiment in the stored definition;
            # remotely-evaluated flags carry it in the response metadata. None when
            # the server (older deployment) does not report it.
            has_experiment: Optional[bool] = None
            # Source the gate the same way as has_experiment above; see
            # _capture_feature_flag_called_if_needed for why.
            minimal_flag_called_events = self._minimal_flag_called_events
            if flag_was_locally_evaluated:
                local_def = (self.feature_flags_by_key or {}).get(key)
                if isinstance(local_def, dict):
                    has_experiment = _parse_has_experiment(
                        local_def.get("has_experiment")
                    )
            elif isinstance(flag_details, FeatureFlag):
                has_experiment = _metadata_has_experiment(flag_details.metadata)
                minimal_flag_called_events = remote_minimal_flag_called_events

            self._capture_feature_flag_called(
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
                has_experiment,
                minimal_flag_called_events,
            )

        return flag_result

    def get_feature_flag_result(
        self,
        key: str,
        distinct_id: ID_TYPES,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = True,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[FeatureFlagResult]:
        """
        Get a FeatureFlagResult object which contains the flag result and payload for a key by evaluating locally or remotely
        depending on whether local evaluation is enabled and the flag can be locally evaluated.
        This also captures the `$feature_flag_called` event unless `send_feature_flag_events` is `False`.

        Examples:
            ```python
            flag_result = posthog.get_feature_flag_result('flag-key', 'distinct_id_of_your_user')
            if flag_result and flag_result.get_value() == 'variant-key':
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = flag_result.payload
            ```

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.
            device_id: The device ID for this request.

        Returns:
            Optional[FeatureFlagResult]: The feature flag result or None if disabled/not found.
        """
        return self._get_feature_flag_result(
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

    def get_feature_flag(
        self,
        key: str,
        distinct_id: ID_TYPES,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        send_feature_flag_events: bool = True,
        disable_geoip: Optional[bool] = None,
        device_id: Optional[str] = None,
    ) -> Optional[FlagValue]:
        """
        Get multivariate feature flag value for a user.

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.
            device_id: The device ID for this request.

        Examples:
            ```python
            enabled_variant = posthog.get_feature_flag('flag-key', 'distinct_id_of_your_user')
            if enabled_variant == 'variant-key': # replace 'variant-key' with the key of your variant
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
            ```

        Category:
            Feature flags
        """
        warnings.warn(
            "`get_feature_flag` is deprecated and will be removed in a future major version. "
            "Use `posthog.evaluate_flags(distinct_id, ...)` and call `flags.get_flag(key)` "
            "instead — this consolidates flag evaluation into a single `/flags` request per "
            "incoming request.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Bypass the public `get_feature_flag_result` so the user only sees one deprecation
        # warning per call.
        feature_flag_result = self._get_feature_flag_result(
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
        return feature_flag_result.get_value() if feature_flag_result else None

    def _locally_evaluate_flag(
        self,
        key: str,
        distinct_id: ID_TYPES,
        groups: Mapping[str, Union[str, int]],
        person_properties: dict[str, str],
        group_properties: dict[str, dict[str, Any]],
        device_id: Optional[str] = None,
    ) -> Optional[FlagValue]:
        if self.feature_flags is None and self.personal_api_key:
            self.load_feature_flags()
        response = None

        if self.feature_flags:
            assert self.feature_flags_by_key is not None, (
                "feature_flags_by_key should be initialized when feature_flags is set"
            )
            # Local evaluation
            flag = self.feature_flags_by_key.get(key)
            if flag:
                try:
                    response = self._compute_flag_locally(
                        flag,
                        distinct_id,
                        groups=groups,
                        person_properties=person_properties,
                        group_properties=group_properties,
                        device_id=device_id,
                    )
                    self.log.debug(
                        f"Successfully computed flag locally: {key} -> {response}"
                    )
                except (RequiresServerEvaluation, InconclusiveMatchError) as e:
                    self.log.debug(f"Failed to compute flag {key} locally: {e}")
                except Exception as e:
                    self.log.exception(
                        f"[FEATURE FLAGS] Error while computing variant locally: {e}"
                    )
        return response

    def get_feature_flag_payload(
        self,
        key: str,
        distinct_id: ID_TYPES,
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
        """
        Get the payload for a feature flag.

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            match_value: The specific flag value to get payload for.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Deprecated. Use get_feature_flag() instead if you need events.
            disable_geoip: Whether to disable GeoIP for this request.
            device_id: The device ID for this request.

        Returns:
            The payload associated with the matched feature flag value, or None.
            This method returns the payload only, not the FeatureFlagResult wrapper
            used internally to compute it.

        Examples:
            ```python
            is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')

            if is_my_flag_enabled:
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
            ```

        Category:
            Feature flags
        """
        warnings.warn(
            "`get_feature_flag_payload` is deprecated and will be removed in a future major "
            "version. Use `posthog.evaluate_flags(distinct_id, ...)` and call "
            "`flags.get_flag_payload(key)` instead — this consolidates flag evaluation into "
            "a single `/flags` request per incoming request.",
            DeprecationWarning,
            stacklevel=2,
        )
        if send_feature_flag_events:
            warnings.warn(
                "send_feature_flag_events is deprecated in get_feature_flag_payload() and will be removed "
                "in a future version. Use get_feature_flag() if you want to send $feature_flag_called events.",
                DeprecationWarning,
                stacklevel=2,
            )

        feature_flag_result = self._get_feature_flag_result(
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
        return feature_flag_result.payload if feature_flag_result else None

    def _get_feature_flag_details_from_server(
        self,
        key: str,
        distinct_id: ID_TYPES,
        groups: Mapping[str, Union[str, int]],
        person_properties: dict[str, str],
        group_properties: dict[str, dict[str, Any]],
        disable_geoip: Optional[bool],
        device_id: Optional[str] = None,
    ) -> tuple[Optional[FeatureFlag], Optional[str], Optional[int], bool, bool]:
        """
        Calls /flags and returns the flag details, request id, evaluated at timestamp,
        whether there were errors while computing flags, and this response's own
        minimal-flag-called-events gate (see _capture_feature_flag_called_if_needed
        for why the caller should use this over the client-wide gate).
        """
        resp_data = self._get_flags_decision(
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
        minimal_flag_called_events = resp_data.get("minimalFlagCalledEvents") is True
        flags = resp_data.get("flags")
        flag_details = flags.get(key) if flags else None
        return (
            flag_details,
            request_id,
            evaluated_at,
            errors_while_computing,
            minimal_flag_called_events,
        )

    def _capture_feature_flag_called(
        self,
        distinct_id: ID_TYPES,
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
        has_experiment: Optional[bool] = None,
        minimal_flag_called_events: bool = False,
    ):
        properties: dict[str, Any] = {
            "$feature_flag": key,
            "$feature_flag_response": response,
            "locally_evaluated": flag_was_locally_evaluated,
            f"$feature/{key}": response,
        }

        if payload is not None:
            # if payload is not a string, json serialize it to a string
            properties["$feature_flag_payload"] = payload

        if request_id:
            properties["$feature_flag_request_id"] = request_id
        if evaluated_at:
            properties["$feature_flag_evaluated_at"] = evaluated_at
        if isinstance(flag_details, FeatureFlag):
            if flag_details.reason and flag_details.reason.description:
                properties["$feature_flag_reason"] = flag_details.reason.description
            if isinstance(flag_details.metadata, FlagMetadata):
                if flag_details.metadata.version:
                    properties["$feature_flag_version"] = flag_details.metadata.version
                if flag_details.metadata.id:
                    properties["$feature_flag_id"] = flag_details.metadata.id
        if feature_flag_error:
            properties["$feature_flag_error"] = feature_flag_error

        self._capture_feature_flag_called_if_needed(
            distinct_id=distinct_id,
            key=key,
            response=response,
            properties=properties,
            groups=groups,
            disable_geoip=disable_geoip,
            has_experiment=has_experiment,
            minimal_flag_called_events=minimal_flag_called_events,
        )

    def _capture_feature_flag_called_if_needed(
        self,
        *,
        distinct_id: ID_TYPES,
        key: str,
        response: Optional[FlagValue],
        properties: dict[str, Any],
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        disable_geoip: Optional[bool] = None,
        has_experiment: Optional[bool] = None,
        minimal_flag_called_events: bool = False,
    ) -> None:
        """Fire a ``$feature_flag_called`` event if the (distinct_id, flag, response,
        groups) tuple hasn't already been reported on this client. Group context is
        included so that group-scoped flags fire a separate event for each group a user
        is evaluated under. Shared by the single-flag evaluation path and
        ``FeatureFlagEvaluations.is_enabled() / get_flag()`` so both paths dedupe
        identically.

        ``has_experiment`` is the server-reported signal for whether the flag is linked
        to an experiment (``None`` when the server did not report it). When the
        server-controlled gate is on and the flag is known non-experiment, the event is
        trimmed to a strict allowlist; any missing signal sends the full legacy shape,
        and experiment-linked flags keep the full set for exposure analysis.

        ``minimal_flag_called_events`` is the gate as observed by the evaluation that
        produced ``response``, not a fresh read of client-wide state: both callers
        resolve it themselves (the snapshot pins it at construction; the single-flag
        path reads it from the specific local/remote source that produced the value)
        so a concurrent poller refresh or another ``/flags`` call can't reshape an
        event after the fact.
        """
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

        # Record the server's experiment signal when known, so minimization's impact
        # can be measured by segmenting on it.
        if has_experiment is not None:
            properties["$feature_flag_has_experiment"] = has_experiment

        # Minimize iff the server-controlled gate is on AND the flag is known to have
        # no linked experiment. Any missing signal (gate absent, has_experiment
        # missing) fails safe to the full legacy shape.
        should_minimize = minimal_flag_called_events and has_experiment is False
        # Only thread the internal allowlist through when minimizing, so the
        # full-property path's capture() call signature stays unchanged.
        extra_capture_kwargs: dict[str, Any] = {}
        if should_minimize:
            extra_capture_kwargs["_property_allowlist"] = (
                _MINIMAL_FLAG_CALLED_EVENT_PROPERTIES
            )

        self.capture(
            "$feature_flag_called",
            distinct_id=distinct_id,
            properties=properties,
            groups=groups or {},
            disable_geoip=disable_geoip,
            **extra_capture_kwargs,
        )
        reported_flags.add(feature_flag_reported_key)

    def get_remote_config_payload(self, key: str):
        """
        Get the payload for a remote config feature flag.

        Args:
            key: The remote config feature flag key.

        Returns:
            The payload associated with the feature flag, or ``None`` if the
            client is disabled, no personal API key is configured, or the request
            fails. Encrypted payloads are decrypted by PostHog before being
            returned.

        Note:
            Requires ``secret_key`` for authentication.

        Category:
            Feature flags
        """
        if self.disabled:
            return None

        if self.personal_api_key is None:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a secret_key to fetch decrypted feature flag payloads."
            )
            return None

        try:
            return remote_config(
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

    def _compute_payload_locally(
        self, key: str, match_value: FlagValue
    ) -> Optional[str]:
        payload = None

        if self.feature_flags_by_key is None:
            return payload

        flag_definition = self.feature_flags_by_key.get(key)
        if flag_definition:
            flag_filters = flag_definition.get("filters") or {}
            flag_payloads = flag_filters.get("payloads") or {}
            # For boolean flags, use lowercase keys ("true" or "false")
            # For multivariate flags, use the variant string as-is
            lookup_value = (
                str(match_value).lower()
                if isinstance(match_value, bool)
                else str(match_value)
            )
            payload = flag_payloads.get(lookup_value, None)
        return payload

    def get_all_flags(
        self,
        distinct_id: ID_TYPES,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> Optional[dict[str, Union[bool, str]]]:
        """
        Get all feature flags for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            disable_geoip: Whether to disable GeoIP for this request.
            flag_keys_to_evaluate: A list of specific flag keys to evaluate. If provided,
                only these flags will be evaluated, improving performance.
            device_id: The device ID for this request.

        Examples:
            ```python
            posthog.get_all_flags('distinct_id_of_your_user')
            ```

        Category:
            Feature flags
        """
        response = self.get_all_flags_and_payloads(
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

    def get_all_flags_and_payloads(
        self,
        distinct_id: ID_TYPES,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        disable_geoip: Optional[bool] = None,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> FlagsAndPayloads:
        """
        Get all feature flags and their payloads for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            disable_geoip: Whether to disable GeoIP for this request.
            flag_keys_to_evaluate: A list of specific flag keys to evaluate. If provided,
                only these flags will be evaluated, improving performance.
            device_id: The device ID for this request.

        Examples:
            ```python
            posthog.get_all_flags_and_payloads('distinct_id_of_your_user')
            ```

        Category:
            Feature flags
        """
        if self.disabled:
            return {"featureFlags": None, "featureFlagPayloads": None}

        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                groups, person_properties, group_properties
            )
        )

        # Resolve device_id from context if not provided
        if device_id is None:
            device_id = get_context_device_id()

        groups = groups or {}

        local_person_properties = self._person_properties_for_local_evaluation(
            distinct_id, person_properties
        )
        response, fallback_to_flags = self._get_all_flags_and_payloads_locally(
            distinct_id,
            groups=groups,
            person_properties=local_person_properties,
            group_properties=group_properties,
            flag_keys_to_evaluate=flag_keys_to_evaluate,
            device_id=device_id,
        )

        if fallback_to_flags and not only_evaluate_locally:
            try:
                decide_response = self._get_flags_decision(
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

    def evaluate_flags(
        self,
        distinct_id: Optional[ID_TYPES] = None,
        *,
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        person_properties: Optional[Dict[str, Any]] = None,
        group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
        only_evaluate_locally: bool = False,
        disable_geoip: Optional[bool] = None,
        flag_keys: Optional[List[str]] = None,
        device_id: Optional[str] = None,
    ) -> FeatureFlagEvaluations:
        """Evaluate all feature flags for a user in a single call and return a
        :class:`FeatureFlagEvaluations` snapshot. Branch on ``.is_enabled()`` /
        ``.get_flag()`` and pass the same snapshot to :meth:`capture` via the
        ``flags`` option so events carry the exact flag values the code branched on.

        Prefer this over repeated ``get_feature_flag()`` calls and over
        ``capture(send_feature_flags=True)`` — it consolidates flag evaluation into
        a single ``/flags`` request per incoming request.

        Local evaluation is transparent: when the poller resolves a flag, the
        snapshot's ``$feature_flag_called`` events are tagged ``locally_evaluated=True``
        and reason ``"Evaluated locally"``.

        Args:
            distinct_id: The user's distinct ID. If ``None``, falls back to the
                context distinct_id. If still unresolvable, returns an empty snapshot.
            groups: Mapping of group type to group key.
            person_properties: Person properties to use for evaluation.
            group_properties: Group properties keyed by group type.
            only_evaluate_locally: If True, never fall back to remote evaluation —
                flags that can't be evaluated locally are simply omitted from the snapshot.
            disable_geoip: Whether to disable GeoIP lookup.
            flag_keys: Optional list that scopes local evaluation, the underlying
                ``/flags`` request, and the returned snapshot. When omitted or ``None``, all
                flags are evaluated. An empty list returns an empty snapshot without evaluating
                flags. A requested key absent from loaded local definitions is included in one
                remote fallback per ``evaluate_flags`` call unless ``only_evaluate_locally`` is
                True. If the server also does not know the key, it is omitted from the snapshot.
            device_id: Optional device ID override. If not provided, falls back to the
                context device_id (which may be set via tracing headers). Used by
                experience-continuity flags to match users across distinct_id changes.

        Returns:
            A :class:`FeatureFlagEvaluations` snapshot.

        Examples:
            ```python
            flags = posthog.evaluate_flags(
                "user_123",
                person_properties={"plan": "enterprise"},
            )
            if flags.is_enabled("new-dashboard"):
                render_new_dashboard()
            posthog.capture("page_viewed", distinct_id="user_123", flags=flags)
            ```

        Category:
            Feature flags
        """
        host = self._get_feature_flag_evaluations_host()

        if distinct_id is None:
            distinct_id = get_context_distinct_id()

        # Resolve device_id from context when not explicitly provided. The context value
        # may be set via tracing headers; the explicit parameter is an override for callers
        # who want to bypass it. Used by the remote /flags request for experience-continuity
        # flag matching.
        if device_id is None:
            device_id = get_context_device_id()

        if not distinct_id or self.disabled:
            # Empty snapshot. The class short-circuits on empty distinct_id so calling
            # is_enabled()/get_flag() on it won't emit events.
            return FeatureFlagEvaluations(host=host, distinct_id="", flags={})

        if flag_keys == []:
            return FeatureFlagEvaluations(
                host=host,
                distinct_id=str(distinct_id),
                flags={},
                groups=groups,
                disable_geoip=disable_geoip,
            )

        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                groups or {},
                person_properties or {},
                group_properties or {},
            )
        )
        groups = groups or {}
        requested_keys = set(flag_keys) if flag_keys else None

        records: Dict[str, _EvaluatedFlagRecord] = {}
        request_id: Optional[str] = None
        evaluated_at: Optional[int] = None
        errors_while_computing = False
        quota_limited = False
        locally_evaluated_keys: set[str] = set()
        # Source the gate the same way as has_experiment below; see
        # _capture_feature_flag_called_if_needed for why. Defaults to the poller's
        # current state; a successful remote fallback overwrites it with that
        # response's own field below.
        minimal_flag_called_events = self._minimal_flag_called_events

        # Try local evaluation first when the poller has loaded definitions.
        local_person_properties = self._person_properties_for_local_evaluation(
            distinct_id, person_properties
        )
        local_result, fallback_to_server = self._get_all_flags_and_payloads_locally(
            distinct_id,
            groups=dict(groups),
            person_properties=local_person_properties,
            group_properties=group_properties,
            flag_keys_to_evaluate=flag_keys,
            device_id=device_id,
        )

        feature_flags_by_key: Dict[str, Any] = self.feature_flags_by_key or {}
        local_flags = local_result.get("featureFlags") or {}
        local_payloads = local_result.get("featureFlagPayloads") or {}
        if requested_keys and not requested_keys.issubset(local_flags):
            # A requested flag may have been created since the last definitions poll.
            # Ask /flags for the caller's original scope unless this is a local-only call.
            fallback_to_server = True

        for key, value in local_flags.items():
            flag_def = feature_flags_by_key.get(key) or {}
            records[key] = _EvaluatedFlagRecord(
                key=key,
                enabled=value is not False,
                variant=value if isinstance(value, str) else None,
                payload=_parse_flag_payload(local_payloads.get(key)),
                id=flag_def.get("id"),
                # The local-evaluation flag definition does not carry a version field;
                # only the remote ``/flags`` response does via ``metadata.version``.
                version=None,
                reason="Evaluated locally",
                locally_evaluated=True,
                has_experiment=_parse_has_experiment(flag_def.get("has_experiment")),
            )
            locally_evaluated_keys.add(key)

        # Fall back to remote evaluation for any flags the poller couldn't resolve locally.
        # Use the flags decision path directly so the resulting records carry id/version/reason
        # and fired ``$feature_flag_called`` events match what ``get_feature_flag()`` emits.
        if fallback_to_server and not only_evaluate_locally:
            try:
                response = self._get_flags_decision(
                    distinct_id,
                    groups=groups,
                    person_properties=person_properties,
                    group_properties=group_properties,
                    disable_geoip=disable_geoip,
                    flag_keys_to_evaluate=flag_keys,
                    device_id=device_id,
                )
                request_id = response.get("requestId")
                raw_evaluated_at = response.get("evaluatedAt")
                evaluated_at = (
                    raw_evaluated_at if isinstance(raw_evaluated_at, int) else None
                )
                errors_while_computing = bool(
                    response.get("errorsWhileComputingFlags", False)
                )
                minimal_flag_called_events = (
                    response.get("minimalFlagCalledEvents") is True
                )
                for key, detail in response.get("flags", {}).items():
                    if requested_keys is not None and key not in requested_keys:
                        continue
                    if key in locally_evaluated_keys:
                        continue
                    payload = _parse_flag_payload(
                        detail.metadata.payload
                        if isinstance(detail.metadata, FlagMetadata)
                        else getattr(detail.metadata, "payload", None)
                    )
                    records[key] = _EvaluatedFlagRecord(
                        key=key,
                        enabled=detail.enabled,
                        variant=detail.variant,
                        payload=payload,
                        id=(
                            detail.metadata.id
                            if isinstance(detail.metadata, FlagMetadata)
                            else None
                        ),
                        version=(
                            detail.metadata.version
                            if isinstance(detail.metadata, FlagMetadata)
                            else None
                        ),
                        reason=(
                            detail.reason.description
                            if detail.reason and detail.reason.description
                            else None
                        ),
                        locally_evaluated=False,
                        has_experiment=_metadata_has_experiment(detail.metadata),
                    )
            except QuotaLimitError as e:
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
            minimal_flag_called_events=minimal_flag_called_events,
        )

    _feature_flag_evaluations_host_cache: Optional[_FeatureFlagEvaluationsHost] = None

    def _get_feature_flag_evaluations_host(self) -> _FeatureFlagEvaluationsHost:
        if self._feature_flag_evaluations_host_cache is None:
            self._feature_flag_evaluations_host_cache = _FeatureFlagEvaluationsHost(
                capture_flag_called_event_if_needed=self._capture_feature_flag_called_if_needed,
                log_warning=lambda message: self.log.warning(message),
            )
        return self._feature_flag_evaluations_host_cache

    def _get_all_flags_and_payloads_locally(
        self,
        distinct_id: ID_TYPES,
        *,
        groups: Mapping[str, Union[str, int]],
        person_properties=None,
        group_properties=None,
        warn_on_unknown_groups=False,
        flag_keys_to_evaluate: Optional[list[str]] = None,
        device_id: Optional[str] = None,
    ) -> tuple[FlagsAndPayloads, bool]:
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        if self.feature_flags is None and self.personal_api_key:
            self.load_feature_flags()

        flags: dict[str, FlagValue] = {}
        payloads: dict[str, str] = {}
        fallback_to_flags = False
        # If loading in previous line failed
        if self.feature_flags:
            # Filter flags based on flag_keys_to_evaluate if provided
            flags_to_process = self.feature_flags
            if flag_keys_to_evaluate:
                flag_keys_set = set(flag_keys_to_evaluate)
                flags_to_process = [
                    flag for flag in self.feature_flags if flag["key"] in flag_keys_set
                ]

            for flag in flags_to_process:
                try:
                    flags[flag["key"]] = self._compute_flag_locally(
                        flag,
                        distinct_id,
                        groups=groups,
                        person_properties=person_properties,
                        group_properties=group_properties,
                        warn_on_unknown_groups=warn_on_unknown_groups,
                        device_id=device_id,
                    )
                    matched_payload = self._compute_payload_locally(
                        flag["key"], flags[flag["key"]]
                    )
                    if matched_payload is not None:
                        payloads[flag["key"]] = matched_payload
                except InconclusiveMatchError:
                    # No need to log this, since it's just telling us to fall back to `/flags`
                    fallback_to_flags = True
                except Exception as e:
                    self.log.exception(
                        f"[FEATURE FLAGS] Error while computing variant and payload: {e}"
                    )
                    fallback_to_flags = True
        else:
            fallback_to_flags = True

        return {
            "featureFlags": flags,
            "featureFlagPayloads": payloads,
        }, fallback_to_flags

    def _initialize_flag_cache(self, cache_url):
        """Initialize feature flag cache for graceful degradation during service outages.

        When enabled, the cache stores flag evaluation results and serves them as fallback
        when the PostHog API is unavailable. This ensures your application continues to
        receive flag values even during outages.

        Args:
            cache_url: Cache configuration URL. Examples:
                - None: Disable caching
                - "memory://local/?ttl=300&size=10000": Memory cache with TTL and size
                - "redis://localhost:6379/0/?ttl=300": Redis cache with TTL
                - "redis://username:password@host:port/?ttl=300": Redis with auth

        Example usage:
            # Memory cache
            client = Client(
                "your-api-key",
                flag_fallback_cache_url="memory://local/?ttl=300&size=10000"
            )

            # Redis cache
            client = Client(
                "your-api-key",
                flag_fallback_cache_url="redis://localhost:6379/0/?ttl=300"
            )

            # Normal evaluation - cache is populated
            flag_value = client.get_feature_flag("my-flag", "user123")

            # During API outage - returns cached value instead of None
            flag_value = client.get_feature_flag("my-flag", "user123")  # Uses cache
        """
        if not cache_url:
            return None

        from urllib.parse import parse_qs, urlparse

        try:
            parsed = urlparse(cache_url)
            scheme = parsed.scheme.lower()
            query_params = parse_qs(parsed.query)
            ttl = int(query_params.get("ttl", [300])[0])

            if scheme == "memory":
                size = int(query_params.get("size", [10000])[0])
                return FlagCache(size, ttl)

            elif scheme == "redis":
                try:
                    # Not worth importing redis if we're not using it
                    import redis

                    redis_url = f"{parsed.scheme}://"
                    if parsed.username or parsed.password:
                        redis_url += f"{parsed.username or ''}:{parsed.password or ''}@"
                    redis_url += (
                        f"{parsed.hostname or 'localhost'}:{parsed.port or 6379}"
                    )
                    if parsed.path:
                        redis_url += parsed.path

                    client = redis.from_url(redis_url)

                    # Test connection before using it
                    client.ping()

                    return RedisFlagCache(client, default_ttl=ttl)

                except ImportError:
                    self.log.warning(
                        "[FEATURE FLAGS] Redis not available, flag caching disabled"
                    )
                    return None
                except Exception as e:
                    self.log.warning(
                        f"[FEATURE FLAGS] Redis connection failed: {e}, flag caching disabled"
                    )
                    return None
            else:
                raise ValueError(
                    f"Unknown cache URL scheme: {scheme}. Supported schemes: memory, redis"
                )

        except Exception as e:
            self.log.warning(
                f"[FEATURE FLAGS] Failed to parse cache URL '{cache_url}': {e}"
            )
            return None

    def feature_flag_definitions(self):
        """
        Return feature flag definitions loaded for local evaluation.

        Returns:
            The currently loaded feature flag definitions, or ``None`` before
            local evaluation has loaded definitions.

        Category:
            Feature flags
        """
        return self.feature_flags

    def _person_properties_for_local_evaluation(self, distinct_id, person_properties):
        local_person_properties = dict(person_properties or {})
        local_person_properties.setdefault("distinct_id", distinct_id)
        return local_person_properties

    def _add_local_person_and_group_properties(
        self, groups, person_properties, group_properties
    ):
        person_properties = person_properties or {}

        all_group_properties = {}
        if groups:
            for group_name in groups:
                all_group_properties[group_name] = {
                    "$group_key": groups[group_name],
                    **((group_properties or {}).get(group_name) or {}),
                }

        return person_properties, all_group_properties


def stringify_id(val):
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return str(val)
