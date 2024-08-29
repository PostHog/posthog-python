import logging
import sys
import threading
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

from posthog.exception_utils import exceptions_from_error_tuple, handle_in_app
from posthog.utils import remove_trailing_slash

if TYPE_CHECKING:
    from posthog.client import Client


class Integrations(str, Enum):
    Django = "django"


DEFAULT_DISTINCT_ID = "python-exceptions"


class ExceptionCapture:
    # TODO: Add client side rate limiting to prevent spamming the server with exceptions

    log = logging.getLogger("posthog")

    def __init__(self, client: "Client", integrations: Optional[List[Integrations]] = None):
        self.client = client
        self.original_excepthook = sys.excepthook
        sys.excepthook = self.exception_handler
        threading.excepthook = self.thread_exception_handler
        self.enabled_integrations = []

        for integration in integrations or []:
            # TODO: Maybe find a better way of enabling integrations
            # This is very annoying currently if we had to add any configuration per integration
            if integration == Integrations.Django:
                try:
                    from posthog.exception_integrations.django import DjangoIntegration

                    enabled_integration = DjangoIntegration(self.exception_receiver)
                    self.enabled_integrations.append(enabled_integration)
                except Exception as e:
                    self.log.exception(f"Failed to enable Django integration: {e}")

    def exception_handler(self, exc_type, exc_value, exc_traceback):
        # don't affect default behaviour.
        self.capture_exception(exc_type, exc_value, exc_traceback)
        self.original_excepthook(exc_type, exc_value, exc_traceback)

    def thread_exception_handler(self, args):
        self.capture_exception(args.exc_type, args.exc_value, args.exc_traceback)

    def exception_receiver(self, exc_info, extra_properties):
        if "distinct_id" in extra_properties:
            metadata = {"distinct_id": extra_properties["distinct_id"]}
        else:
            metadata = None
        self.capture_exception(exc_info[0], exc_info[1], exc_info[2], metadata)

    def capture_exception(self, exc_type, exc_value, exc_traceback, metadata=None):
        try:
            # if hasattr(sys, "ps1"):
            #     # Disable the excepthook for interactive Python shells
            #     return

            # Format stack trace like sentry
            all_exceptions_with_trace = exceptions_from_error_tuple((exc_type, exc_value, exc_traceback))

            # Add in-app property to frames in the exceptions
            event = handle_in_app(
                {
                    "exception": {
                        "values": all_exceptions_with_trace,
                    },
                }
            )
            all_exceptions_with_trace_and_in_app = event["exception"]["values"]

            distinct_id = metadata.get("distinct_id") if metadata else DEFAULT_DISTINCT_ID
            # Make sure we have a distinct_id if its empty in metadata
            distinct_id = distinct_id or DEFAULT_DISTINCT_ID

            properties = {
                "$exception_type": all_exceptions_with_trace_and_in_app[0].get("type"),
                "$exception_message": all_exceptions_with_trace_and_in_app[0].get("value"),
                "$exception_list": all_exceptions_with_trace_and_in_app,
                "$exception_personURL": f"{remove_trailing_slash(self.client.raw_host)}/project/{self.client.api_key}/person/{distinct_id}",
            }

            # TODO: What distinct id should we attach these server-side exceptions to?
            # Any heuristic seems prone to errors - how can we know if exception occurred in the context of a user that captured some other event?

            self.client.capture(distinct_id, "$exception", properties=properties)
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")
