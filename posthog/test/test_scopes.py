import unittest
from unittest.mock import patch

from posthog.scopes import new_context, tag, get_tags, clear_tags, tracked


class TestScopes(unittest.TestCase):
    def setUp(self):
        # Reset any context between tests
        clear_tags()

    def test_tag_and_get_tags(self):
        tag("key1", "value1")
        tag("key2", 2)

        tags = get_tags()
        self.assertEqual(tags["key1"], "value1")
        self.assertEqual(tags["key2"], 2)

    def test_clear_tags(self):
        tag("key1", "value1")
        self.assertEqual(get_tags()["key1"], "value1")

        clear_tags()
        self.assertEqual(get_tags(), {})

    def test_new_context_isolation(self):
        # Set tag in outer context
        tag("outer", "value")

        with new_context():
            # Inner context should start empty
            self.assertEqual(get_tags(), {})

            # Set tag in inner context
            tag("inner", "value")
            self.assertEqual(get_tags()["inner"], "value")

            # Outer tag should not be visible
            self.assertNotIn("outer", get_tags())

        # After exiting context, inner tag should be gone
        self.assertNotIn("inner", get_tags())

        # Outer tag should still be there
        self.assertEqual(get_tags()["outer"], "value")

    def test_nested_contexts(self):
        tag("level1", "value1")

        with new_context():
            tag("level2", "value2")

            with new_context():
                tag("level3", "value3")
                self.assertEqual(get_tags(), {"level3": "value3"})

            # Back to level 2
            self.assertEqual(get_tags(), {"level2": "value2"})

        # Back to level 1
        self.assertEqual(get_tags(), {"level1": "value1"})

    @patch("posthog.capture_exception")
    def test_tracked_decorator_success(self, mock_capture):
        @tracked
        def successful_function(x, y):
            tag("x", x)
            tag("y", y)
            return x + y

        result = successful_function(1, 2)

        # Function should execute normally
        self.assertEqual(result, 3)

        # No exception should be captured
        mock_capture.assert_not_called()

        # Context should be cleared after function execution
        self.assertEqual(get_tags(), {})

    @patch("posthog.capture_exception")
    def test_tracked_decorator_exception(self, mock_capture):
        test_exception = ValueError("Test exception")

        @tracked
        def failing_function():
            tag("important_context", "value")
            raise test_exception

        # Function should raise the exception
        with self.assertRaises(ValueError):
            failing_function()

        # Exception should be captured with context
        mock_capture.assert_called_once()
        args, kwargs = mock_capture.call_args

        # Check that the exception was passed
        self.assertEqual(args[0], test_exception)

        # Check that the context was included in properties
        self.assertEqual(kwargs.get("properties", {}).get("important_context"), "value")

        # Context should be cleared after function execution
        self.assertEqual(get_tags(), {})
