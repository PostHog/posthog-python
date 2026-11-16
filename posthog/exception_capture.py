# Portions of this file are derived from getsentry/sentry-python
# Copyright (c) 2018 Functional Software, Inc. dba Sentry
# Licensed under the MIT License: https://github.com/getsentry/sentry-python/blob/master/LICENSE

# 💖open source (under MIT License)

import logging
import sys
import threading
from typing import TYPE_CHECKING, Optional

from posthog.bucketed_rate_limiter import BucketedRateLimiter
from .exception_utils import (
    _ExceptionCaptureMetadata,
    _capture_exception_with_metadata,
)

if TYPE_CHECKING:
    from posthog.client import Client


class ExceptionCapture:
    """Installs process-wide exception hooks that forward uncaught exceptions to PostHog.

    Hooks are installed on :attr:`sys.excepthook` (main-thread unhandled exceptions) and
    :attr:`threading.excepthook` (exceptions that escape worker threads). The original
    hooks are restored when :meth:`close` is called.

    Optionally enforces client-side rate limiting per exception type so that a sudden
    burst of identical errors does not flood the PostHog ingestion pipeline.
    """

    log = logging.getLogger("posthog")

    # more generous defaults than the browser SDK (10, 1, 10) because one
    # server process aggregates exceptions across many users' requests
    DEFAULT_BUCKET_SIZE = 50
    DEFAULT_REFILL_RATE = 10
    DEFAULT_REFILL_INTERVAL_SECONDS = 10

    def __init__(
        self,
        client: "Client",
        rate_limiting_enabled=False,
        bucket_size=DEFAULT_BUCKET_SIZE,
        refill_rate=DEFAULT_REFILL_RATE,
        refill_interval_seconds=DEFAULT_REFILL_INTERVAL_SECONDS,
    ):
        """Install exception hooks and configure optional rate limiting.

        Args:
            client: The PostHog :class:`~posthog.client.Client` used to send captured exceptions.
            rate_limiting_enabled: When ``True``, a :class:`~posthog.bucketed_rate_limiter.BucketedRateLimiter`
                is created with the given bucket parameters to suppress excess captures of the same
                exception type.
            bucket_size: Token capacity of the rate-limiting bucket per exception type.
                Because the call that empties the bucket is itself treated as rate-limited,
                a fresh bucket permits ``bucket_size - 1`` captures before limiting takes
                effect. Clamped to ``[0, 100]``. Default is :attr:`DEFAULT_BUCKET_SIZE` (``50``).
            refill_rate: Tokens restored per completed ``refill_interval_seconds`` interval.
                Clamped to ``[0, bucket_size]``. Default is :attr:`DEFAULT_REFILL_RATE` (``10``).
            refill_interval_seconds: Duration of each refill interval in seconds.
                Clamped to ``[0, 86400]``. Default is :attr:`DEFAULT_REFILL_INTERVAL_SECONDS` (``10``).
        """
        self.client = client
        self._closed = False
        self.original_excepthook = sys.excepthook
        self._original_threading_excepthook = threading.excepthook
        self._sys_excepthook = self.exception_handler
        self._threading_excepthook = self.thread_exception_handler
        sys.excepthook = self._sys_excepthook
        threading.excepthook = self._threading_excepthook
        # opt-in client-side rate limiting: per exception type, allow a burst
        # of captures, then refill over time
        self._rate_limiter = None
        if rate_limiting_enabled:
            self._rate_limiter = BucketedRateLimiter(
                bucket_size=bucket_size,
                refill_rate=refill_rate,
                refill_interval_seconds=refill_interval_seconds,
            )

    def close(self):
        """Uninstall exception hooks and restore the hooks that were in place at construction time.

        Idempotent: calling :meth:`close` a second time has no effect. After closing,
        the rate limiter (if any) is also stopped and its per-key buckets are cleared.

        This method is intentionally not thread-safe with respect to concurrent :meth:`close`
        calls; callers that share an :class:`ExceptionCapture` across threads should arrange
        external synchronisation if they need to close it concurrently.
        """
        if self._closed:
            return

        self._closed = True
        original_excepthook = self._resolve_hook(
            self.original_excepthook,
            "exception_handler",
            "original_excepthook",
        )
        original_threading_excepthook = self._resolve_hook(
            self._original_threading_excepthook,
            "thread_exception_handler",
            "_original_threading_excepthook",
        )

        # Keep each final ownership check and assignment together without a
        # Python call between them. On supported GIL-enabled CPython builds,
        # ordinary Python thread scheduling has no switch point inside either
        # straight-line pair, minimizing the window for external hook writers.
        if sys.excepthook is self._sys_excepthook:
            sys.excepthook = original_excepthook
        if threading.excepthook is self._threading_excepthook:
            threading.excepthook = original_threading_excepthook

        if self._rate_limiter is not None:
            self._rate_limiter.stop()

    def exception_handler(self, exc_type, exc_value, exc_traceback):
        """Handle an unhandled exception from :attr:`sys.excepthook`.

        Forwards the exception to PostHog with ``level="fatal"`` and
        ``mechanism.type="onuncaughtexception"``, then delegates to the previous
        ``sys.excepthook`` so that normal Python crash output is preserved.

        Args:
            exc_type: The exception class.
            exc_value: The exception instance.
            exc_traceback: The traceback object.
        """
        if not self._closed:
            capture_metadata: _ExceptionCaptureMetadata = {
                "level": "fatal",
                "source": "python.sys_excepthook",
                "mechanism": {
                    "type": "onuncaughtexception",
                    "handled": False,
                },
            }
            self._capture_exception(
                (exc_type, exc_value, exc_traceback),
                capture_metadata=capture_metadata,
            )
        previous_hook = self._resolve_hook(
            self.original_excepthook,
            "exception_handler",
            "original_excepthook",
        )
        previous_hook(exc_type, exc_value, exc_traceback)

    def thread_exception_handler(self, args):
        """Handle an unhandled exception from :attr:`threading.excepthook`.

        Forwards the exception to PostHog with ``level="error"`` and
        ``mechanism.type="onuncaughtexception"``, then delegates to the previous
        ``threading.excepthook``.

        Args:
            args: The :class:`threading.ExceptHookArgs` object supplied by the threading machinery,
                containing ``exc_type``, ``exc_value``, ``exc_traceback``, and ``thread``.
        """
        if not self._closed:
            capture_metadata: _ExceptionCaptureMetadata = {
                "level": "error",
                "source": "python.threading_excepthook",
                "mechanism": {
                    "type": "onuncaughtexception",
                    "handled": False,
                },
            }
            self._capture_exception(
                (args.exc_type, args.exc_value, args.exc_traceback),
                capture_metadata=capture_metadata,
            )
        previous_hook = self._resolve_hook(
            self._original_threading_excepthook,
            "thread_exception_handler",
            "_original_threading_excepthook",
        )
        previous_hook(args)

    @staticmethod
    def _resolve_hook(hook, handler_name, previous_hook_name):
        """Skip closed ExceptionCapture hooks while preserving the hook chain."""
        while True:
            owner = getattr(hook, "__self__", None)
            if not isinstance(owner, ExceptionCapture) or not owner._closed:
                return hook
            if hook != getattr(owner, handler_name):
                return hook
            hook = getattr(owner, previous_hook_name)

    def exception_receiver(self, exc_info, extra_properties):
        """Receive an exception from an external caller and forward it to PostHog.

        Provides an entry point for external callers to pass an ``exc_info`` tuple
        and extra properties (such as ``"distinct_id"``) into the exception capture
        pipeline without going through :attr:`sys.excepthook`.

        Args:
            exc_info: A three-tuple ``(exc_type, exc_value, exc_traceback)``.
            extra_properties: A mapping of extra properties. If it contains a ``"distinct_id"``
                key, that value is used as the PostHog distinct ID for the captured event.
        """
        if "distinct_id" in extra_properties:
            metadata = {"distinct_id": extra_properties["distinct_id"]}
        else:
            metadata = None
        self.capture_exception((exc_info[0], exc_info[1], exc_info[2]), metadata)

    def capture_exception(self, exception, metadata=None):
        """Capture a single exception and send it to PostHog.

        A convenience wrapper around the internal :meth:`_capture_exception` method intended
        for direct call sites that already hold an exception object or ``(type, value, tb)``
        tuple outside of the ``sys.excepthook`` flow.

        Args:
            exception: Either an :class:`Exception` instance or a three-tuple
                ``(exc_type, exc_value, exc_traceback)`` as returned by :func:`sys.exc_info`.
            metadata: Optional mapping with an optional ``"distinct_id"`` key to associate
                the event with a specific PostHog person.
        """
        self._capture_exception(exception, metadata)

    def _capture_exception(
        self,
        exception,
        metadata=None,
        capture_metadata: Optional[_ExceptionCaptureMetadata] = None,
    ):
        try:
            if self._rate_limiter is not None:
                exception_type = self._exception_type(exception)
                if self._rate_limiter.consume_rate_limit(exception_type):
                    self.log.info(
                        f"Skipping exception capture because of client rate limiting. exception={exception_type}"
                    )
                    return

            distinct_id = metadata.get("distinct_id") if metadata else None
            resolved_capture_metadata: _ExceptionCaptureMetadata = (
                capture_metadata or {}
            )
            _capture_exception_with_metadata(
                self.client,
                exception,
                resolved_capture_metadata,
                distinct_id=distinct_id,
            )
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")

    @staticmethod
    def _exception_type(exception):
        if isinstance(exception, tuple):
            exc_info = exception
        else:
            exc_info = (
                type(exception),
                exception,
                getattr(exception, "__traceback__", None),
            )

        # Canonical `$exception_list` order puts the caught/outermost
        # exception first, and server-side issue naming keys on that first
        # entry. Key rate-limit buckets on the same type so they line up
        # (e.g. `raise RuntimeError from ZeroDivisionError` is keyed on
        # RuntimeError, matching the issue it groups into).
        exc_type = exc_info[0]

        return getattr(exc_type, "__name__", None) or "Exception"
