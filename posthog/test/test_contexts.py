import asyncio
import unittest
from unittest.mock import MagicMock, patch

import posthog
from posthog.client import Client
from posthog.contexts import (
    get_tags,
    new_context,
    scoped,
    tag,
    identify_context,
    set_context_session,
    get_context_session_id,
    get_context_distinct_id,
)


class TestContexts(unittest.TestCase):
    def test_tag_and_get_tags(self):
        with new_context(fresh=True):
            tag("key1", "value1")
            tag("key2", 2)

            tags = get_tags()
            assert tags["key1"] == "value1"
            assert tags["key2"] == 2

    def test_new_context_isolation(self):
        with new_context(fresh=True):
            # Set tag in outer context
            tag("outer", "value")

            with new_context(fresh=True):
                # Inner context should start empty
                assert get_tags() == {}

                # Set tag in inner context
                tag("inner", "value")
                assert get_tags()["inner"] == "value"

                # Outer tag should not be visible
                self.assertNotIn("outer", get_tags())

            with new_context(fresh=False):
                # Inner context should inherit outer tag
                assert get_tags() == {"outer": "value"}

            # After exiting context, inner tag should be gone
            self.assertNotIn("inner", get_tags())

            # Outer tag should still be there
            assert get_tags()["outer"] == "value"

    def test_nested_contexts(self):
        with new_context(fresh=True):
            tag("level1", "value1")

            with new_context(fresh=True):
                tag("level2", "value2")

                with new_context(fresh=True):
                    tag("level3", "value3")
                    assert get_tags() == {"level3": "value3"}

                # Back to level 2
                assert get_tags() == {"level2": "value2"}

            # Back to level 1
            assert get_tags() == {"level1": "value1"}

    @patch("posthog.capture_exception")
    def test_scoped_decorator_success(self, mock_capture):
        @scoped()
        def successful_function(x, y):
            tag("x", x)
            tag("y", y)
            return x + y

        result = successful_function(1, 2)

        # Function should execute normally
        assert result == 3

        # No exception should be captured
        mock_capture.assert_not_called()

        # Context should be cleared after function execution
        assert get_tags() == {}

    @patch("posthog.capture_exception")
    def test_scoped_decorator_exception(self, mock_capture):
        def check_context_on_capture(exception, **kwargs):
            # Assert tags are available when capture_exception is called
            current_tags = get_tags()
            assert current_tags.get("important_context") == "value"

        mock_capture.side_effect = check_context_on_capture

        for name, is_async in [("sync", False), ("async", True)]:
            with self.subTest(name=name):
                test_exception = ValueError(f"Test {name} exception")

                if is_async:

                    @scoped(capture_exceptions=True)
                    async def failing_function():
                        tag("important_context", "value")
                        raise test_exception

                    def run():
                        return asyncio.run(failing_function())

                else:

                    @scoped(capture_exceptions=True)
                    def failing_function():
                        tag("important_context", "value")
                        raise test_exception

                    run = failing_function

                # Function should raise the exception
                with self.assertRaises(ValueError):
                    run()

                # Verify capture_exception was called
                mock_capture.assert_called_once_with(test_exception)

                # Context should be cleared after function execution
                assert get_tags() == {}

                mock_capture.reset_mock()

    @patch("posthog.capture_exception")
    def test_new_context_exception_handling(self, mock_capture):
        test_exception = RuntimeError("Context exception")

        def check_context_on_capture(exception, **kwargs):
            # Assert inner context tags are available when capture_exception is called
            current_tags = get_tags()
            assert current_tags.get("inner_context") == "inner_value"

        mock_capture.side_effect = check_context_on_capture

        # Set up outer context
        with new_context():
            tag("outer_context", "outer_value")

            try:
                with new_context(capture_exceptions=True):
                    tag("inner_context", "inner_value")
                    raise test_exception
            except RuntimeError:
                pass  # Expected exception

            # Outer context should still be intact
            assert get_tags()["outer_context"] == "outer_value"

        # Verify capture_exception was called
        mock_capture.assert_called_once_with(test_exception)

    @patch("posthog.capture_exception")
    def test_new_context_defaults_to_global_exception_autocapture_disabled(
        self, mock_capture
    ):
        original_default_client = posthog.default_client
        original_enable_exception_autocapture = posthog.enable_exception_autocapture
        posthog.default_client = None
        posthog.enable_exception_autocapture = False
        test_exception = RuntimeError("Context exception")

        try:
            with self.assertRaises(RuntimeError):
                with posthog.new_context():
                    raise test_exception
        finally:
            posthog.default_client = original_default_client
            posthog.enable_exception_autocapture = original_enable_exception_autocapture

        mock_capture.assert_not_called()

    def test_new_context_defaults_to_custom_client_exception_autocapture_disabled(self):
        client = Client(
            "phc_test",
            sync_mode=True,
            disabled=True,
            enable_exception_autocapture=False,
        )
        client.capture_exception = MagicMock()
        test_exception = RuntimeError("Context exception")

        try:
            with self.assertRaises(RuntimeError):
                with client.new_context():
                    raise test_exception
        finally:
            client.shutdown()

        client.capture_exception.assert_not_called()

    @patch("posthog.capture_exception")
    def test_new_context_explicit_true_captures_when_global_autocapture_disabled(
        self, mock_capture
    ):
        original_default_client = posthog.default_client
        original_enable_exception_autocapture = posthog.enable_exception_autocapture
        posthog.default_client = None
        posthog.enable_exception_autocapture = False
        test_exception = RuntimeError("Context exception")

        try:
            with self.assertRaises(RuntimeError):
                with posthog.new_context(capture_exceptions=True):
                    raise test_exception
        finally:
            posthog.default_client = original_default_client
            posthog.enable_exception_autocapture = original_enable_exception_autocapture

        mock_capture.assert_called_once_with(test_exception)

    @patch("posthog.capture_exception")
    def test_new_context_explicit_false_skips_capture_when_global_autocapture_enabled(
        self, mock_capture
    ):
        original_default_client = posthog.default_client
        original_enable_exception_autocapture = posthog.enable_exception_autocapture
        posthog.default_client = None
        posthog.enable_exception_autocapture = True
        test_exception = RuntimeError("Context exception")

        try:
            with self.assertRaises(RuntimeError):
                with posthog.new_context(capture_exceptions=False):
                    raise test_exception
        finally:
            posthog.default_client = original_default_client
            posthog.enable_exception_autocapture = original_enable_exception_autocapture

        mock_capture.assert_not_called()

    def test_identify_context(self):
        with new_context(fresh=True):
            # Initially no distinct ID
            assert get_context_distinct_id() is None

            # Set distinct ID
            identify_context("user123")
            assert get_context_distinct_id() == "user123"

    def test_set_context_session(self):
        with new_context(fresh=True):
            # Initially no session ID
            assert get_context_session_id() is None

            # Set session ID
            set_context_session("session456")
            assert get_context_session_id() == "session456"

    def test_context_inheritance_fresh_context(self):
        with new_context(fresh=True):
            identify_context("user123")
            set_context_session("session456")

            with new_context(fresh=True):
                # Fresh context should not inherit
                assert get_context_distinct_id() is None
                assert get_context_session_id() is None

            # Original context should still have values
            assert get_context_distinct_id() == "user123"
            assert get_context_session_id() == "session456"

    def test_context_inheritance_non_fresh_context(self):
        with new_context(fresh=True):
            identify_context("user123")
            set_context_session("session456")

            with new_context(fresh=False):
                # Non-fresh context should inherit
                assert get_context_distinct_id() == "user123"
                assert get_context_session_id() == "session456"

                # Override in child context
                identify_context("user789")
                set_context_session("session999")
                assert get_context_distinct_id() == "user789"
                assert get_context_session_id() == "session999"

            # Original context should still have original values
            assert get_context_distinct_id() == "user123"
            assert get_context_session_id() == "session456"

    def test_child_tags_override_parent_tags_in_non_fresh_context(self):
        with new_context(fresh=True):
            tag("shared_key", "parent_value")
            tag("parent_only", "parent")

            with new_context(fresh=False):
                # Child should inherit parent tags
                assert get_tags()["parent_only"] == "parent"

                # Child sets same key - should override parent
                tag("shared_key", "child_value")
                tag("child_only", "child")

                tags = get_tags()
                # Child value should win for shared key
                assert tags["shared_key"] == "child_value"
                # Both parent and child tags should be present
                assert tags["parent_only"] == "parent"
                assert tags["child_only"] == "child"

            # Parent context should be unchanged
            parent_tags = get_tags()
            assert parent_tags["shared_key"] == "parent_value"
            assert parent_tags["parent_only"] == "parent"
            assert "child_only" not in parent_tags

    def test_scoped_decorator_with_context_ids(self):
        @scoped()
        def sync_function_with_context():
            identify_context("user456")
            set_context_session("session789")
            return get_context_distinct_id(), get_context_session_id()

        @scoped()
        async def async_function_with_context():
            identify_context("user456")
            set_context_session("session789")
            return get_context_distinct_id(), get_context_session_id()

        cases = [
            ("sync", sync_function_with_context, lambda func: func()),
            ("async", async_function_with_context, lambda func: asyncio.run(func())),
        ]

        for name, func, run in cases:
            with self.subTest(name=name):
                distinct_id, session_id = run(func)
                assert distinct_id == "user456"
                assert session_id == "session789"

                # Context should be cleared after function execution
                assert get_context_distinct_id() is None
                assert get_context_session_id() is None

    def test_scoped_decorator_async_concurrent_context_isolation(self):
        first_ready = asyncio.Event()
        second_ready = asyncio.Event()
        first_checked = asyncio.Event()

        @scoped()
        async def first():
            identify_context("user_1")
            first_ready.set()
            await second_ready.wait()
            distinct_id = get_context_distinct_id()
            first_checked.set()
            return distinct_id

        @scoped()
        async def second():
            await first_ready.wait()
            identify_context("user_2")
            second_ready.set()
            await first_checked.wait()
            return get_context_distinct_id()

        async def run():
            return await asyncio.wait_for(asyncio.gather(first(), second()), timeout=1)

        assert asyncio.run(run()) == ["user_1", "user_2"]
