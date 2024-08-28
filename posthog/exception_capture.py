import logging
import sys
import threading
from typing import TYPE_CHECKING

from posthog.exception_utils import exceptions_from_error_tuple

if TYPE_CHECKING:
    from posthog.client import Client


class ExceptionCapture:
    # TODO: Add client side rate limiting to prevent spamming the server with exceptions

    log = logging.getLogger("posthog")

    def __init__(self, client: "Client"):
        self.client = client
        self.original_excepthook = sys.excepthook
        sys.excepthook = self.exception_handler
        threading.excepthook = self.thread_exception_handler

    def exception_handler(self, exc_type, exc_value, exc_traceback):
        # don't affect default behaviour.
        self.capture_exception(exc_type, exc_value, exc_traceback)
        self.original_excepthook(exc_type, exc_value, exc_traceback)

    def thread_exception_handler(self, args):
        self.capture_exception(args.exc_type, args.exc_value, args.exc_traceback)

    def capture_exception(self, exc_type, exc_value, exc_traceback):
        try:
            # if hasattr(sys, "ps1"):
            #     # Disable the excepthook for interactive Python shells
            #     return

            # Format stack trace like sentry
            all_exceptions_with_trace = exceptions_from_error_tuple((exc_type, exc_value, exc_traceback))

            properties = {
                "$exception_type": all_exceptions_with_trace[0].get("type"),
                "$exception_message": all_exceptions_with_trace[0].get("value"),
                "$exception_list": all_exceptions_with_trace,
                # TODO: Can we somehow get distinct_id from context here? Stateless lib makes this much harder? 😅
                # '$exception_personURL': f'{self.client.posthog_host}/project/{self.client.token}/person/{self.client.get_distinct_id()}'
            }

            # TODO: What distinct id should we attach these server-side exceptions to?
            # Any heuristic seems prone to errors - how can we know if exception occurred in the context of a user that captured some other event?

            self.client.capture("python-exceptions", "$exception", properties=properties)
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")
