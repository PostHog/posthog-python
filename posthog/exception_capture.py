# Portions of this file are derived from getsentry/sentry-python
# Copyright (c) 2018 Functional Software, Inc. dba Sentry
# Licensed under the MIT License: https://github.com/getsentry/sentry-python/blob/master/LICENSE

# 💖open source (under MIT License)

import logging
import sys
import threading
from typing import TYPE_CHECKING

from posthog.bucketed_rate_limiter import BucketedRateLimiter

if TYPE_CHECKING:
    from posthog.client import Client


class ExceptionCapture:
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
        if not self._closed:
            self.capture_exception((exc_type, exc_value, exc_traceback))
        previous_hook = self._resolve_hook(
            self.original_excepthook,
            "exception_handler",
            "original_excepthook",
        )
        previous_hook(exc_type, exc_value, exc_traceback)

    def thread_exception_handler(self, args):
        if not self._closed:
            self.capture_exception((args.exc_type, args.exc_value, args.exc_traceback))
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
        if "distinct_id" in extra_properties:
            metadata = {"distinct_id": extra_properties["distinct_id"]}
        else:
            metadata = None
        self.capture_exception((exc_info[0], exc_info[1], exc_info[2]), metadata)

    def capture_exception(self, exception, metadata=None):
        try:
            if self._rate_limiter is not None:
                exception_type = self._exception_type(exception)
                if self._rate_limiter.consume_rate_limit(exception_type):
                    self.log.info(
                        f"Skipping exception capture because of client rate limiting. exception={exception_type}"
                    )
                    return

            distinct_id = metadata.get("distinct_id") if metadata else None
            self.client.capture_exception(exception, distinct_id=distinct_id)
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
