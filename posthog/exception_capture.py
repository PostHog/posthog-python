import json
import sys
import threading
from typing import TYPE_CHECKING

from posthog.exception_utils import single_exception_from_error_tuple

if TYPE_CHECKING:
    from posthog.client import Client


class ExceptionCapture:
    # TODO: Some method of avoiding internal SDK exceptions?

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
        # if hasattr(sys, "ps1"):
        #     # Disable the excepthook for interactive Python shells
        #     return

        # Format stack trace like sentry
        # TODO: For now, we don't support exception chaining and groups, just a single top level exception...
        exception_info = single_exception_from_error_tuple(exc_type, exc_value, exc_traceback)
        stack_trace = (
            exception_info["stacktrace"]["frames"]
            if "stacktrace" in exception_info and exception_info["stacktrace"].get("frames")
            else None
        )

        properties = {
            "$exception_type": exc_type.__name__,
            "$exception_message": str(exc_value),
            "$exception_stack_trace_raw": json.dumps(stack_trace),
            # TODO: Can we somehow get distinct_id from context here? Stateless lib makes this much harder? 😅
            # '$exception_personURL': f'{self.client.posthog_host}/project/{self.client.token}/person/{self.client.get_distinct_id()}'
        }

        # TODO: What distinct id should we attach these server-side exceptions to?
        # Any heuristic seems prone to errors - how can we know if exception occurred in the context of a user that captured some other event?

        self.client.capture("python-exceptions", "$exception", properties=properties)
