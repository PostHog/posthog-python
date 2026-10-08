import builtins
import unittest
from unittest.mock import Mock, patch

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-secret-key",
        INSTALLED_APPS=[],
        MIDDLEWARE=[],
    )
    django.setup()

from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from posthog.contexts import new_context, tag
from posthog.integrations.drf import create_exception_handler, exception_handler


class ServiceUnavailable(APIException):
    status_code = 503
    default_detail = "Service unavailable"


class TestDjangoRestFrameworkIntegration(unittest.TestCase):
    def test_default_handler_captures_5xx_with_canonical_metadata(self):
        client = Mock()
        handler = create_exception_handler(client=client)
        exception = ServiceUnavailable()

        response = handler(exception, {})

        self.assertEqual(response.status_code, 503)
        client.capture_exception.assert_called_once_with(
            exception,
            _capture_metadata={
                "level": "error",
                "source": "django_rest_framework.exception_handler",
                "mechanism": {
                    "type": "middleware",
                    "handled": True,
                },
            },
        )

    def test_default_handler_does_not_capture_expected_4xx(self):
        client = Mock()
        handler = create_exception_handler(client=client)
        exception = ValidationError({"name": ["This field is required."]})

        response = handler(exception, {})

        self.assertEqual(response.status_code, 400)
        client.capture_exception.assert_not_called()

    def test_capture_4xx_is_opt_in(self):
        client = Mock()
        handler = create_exception_handler(client=client, capture_4xx=True)
        exception = ValidationError("Invalid input")

        response = handler(exception, {})

        self.assertEqual(response.status_code, 400)
        client.capture_exception.assert_called_once()

    def test_unhandled_exception_is_left_for_django_middleware(self):
        client = Mock()
        handler = create_exception_handler(client=client)
        exception = RuntimeError("unhandled")

        response = handler(exception, {})

        self.assertIsNone(response)
        client.capture_exception.assert_not_called()

    def test_custom_handler_response_and_context_are_preserved(self):
        client = Mock()
        response = Response({"detail": "custom"}, status=502)
        delegate = Mock(return_value=response)
        handler = create_exception_handler(delegate, client=client)
        exception = RuntimeError("upstream failed")
        context = {"view": object(), "request": object()}

        returned_response = handler(exception, context)

        self.assertIs(returned_response, response)
        delegate.assert_called_once_with(exception, context)
        client.capture_exception.assert_called_once()

    def test_custom_handler_exception_is_preserved(self):
        client = Mock()
        handler_error = LookupError("handler failed")
        delegate = Mock(side_effect=handler_error)
        handler = create_exception_handler(delegate, client=client)

        with self.assertRaisesRegex(LookupError, "handler failed"):
            handler(RuntimeError("view failed"), {})

        client.capture_exception.assert_not_called()

    def test_exception_filter_can_suppress_capture(self):
        client = Mock()
        exception_filter = Mock(return_value=False)
        handler = create_exception_handler(
            lambda exc, context: Response(status=500),
            client=client,
            exception_filter=exception_filter,
        )
        exception = RuntimeError("filtered")
        context = {"request": object()}

        handler(exception, context)

        exception_filter.assert_called_once()
        client.capture_exception.assert_not_called()

    def test_already_captured_exception_is_not_captured_twice(self):
        client = Mock()
        handler = create_exception_handler(
            lambda exc, context: Response(status=500), client=client
        )
        exception = RuntimeError("already captured")
        setattr(exception, "__posthog_exception_captured", True)

        handler(exception, {})

        client.capture_exception.assert_not_called()

    def test_capture_runs_inside_existing_django_request_context(self):
        observed_context = []
        client = Mock()

        def capture_exception(*args, **kwargs):
            from posthog.contexts import get_tags

            observed_context.append(get_tags())

        client.capture_exception.side_effect = capture_exception
        handler = create_exception_handler(
            lambda exc, context: Response(status=500), client=client
        )

        with new_context():
            tag("$request_path", "/api/widgets")
            handler(RuntimeError("failed"), {})

        self.assertEqual(observed_context, [{"$request_path": "/api/widgets"}])

    def test_capture_failure_does_not_change_response(self):
        client = Mock()
        client.capture_exception.side_effect = RuntimeError("capture failed")
        response = Response(status=500)
        handler = create_exception_handler(lambda exc, context: response, client=client)

        with self.assertLogs("posthog", level="ERROR"):
            returned_response = handler(RuntimeError("view failed"), {})

        self.assertIs(returned_response, response)

    def test_module_handler_uses_global_client(self):
        exception = ServiceUnavailable()

        with patch("posthog.capture_exception") as capture_exception:
            response = exception_handler(exception, {})

        self.assertEqual(response.status_code, 503)
        capture_exception.assert_called_once_with(
            exception,
            _capture_metadata={
                "level": "error",
                "source": "django_rest_framework.exception_handler",
                "mechanism": {
                    "type": "middleware",
                    "handled": True,
                },
            },
        )

    def test_importing_module_does_not_import_drf(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "rest_framework" or name.startswith("rest_framework."):
                raise AssertionError("DRF imported eagerly")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            # Reloading executes all module-level imports and factory setup.
            import importlib
            import posthog.integrations.drf as drf_integration

            importlib.reload(drf_integration)
