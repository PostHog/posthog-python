import contextvars
from contextlib import contextmanager
from typing import Any, Callable, Dict, TypeVar, cast

_context_stack: contextvars.ContextVar[list] = contextvars.ContextVar(
    "posthog_context_stack", default=[{}]
)


def _get_current_context() -> Dict[str, Any]:
    return _context_stack.get()[-1]


@contextmanager
def new_context(fresh=False, capture_exceptions=True):
    """
    Create a new context scope that will be active for the duration of the with block.
    Any tags set within this scope will be isolated to this context. Any exceptions raised
    or events captured within the context will be tagged with the context tags.

    Args:
        fresh: Whether to start with a fresh context (default: False).
               If False, inherits tags from parent context.
               If True, starts with no tags.
        capture_exceptions: Whether to capture exceptions raised within the context (default: True).
               If True, captures exceptions and tags them with the context tags before propagating them.
               If False, exceptions will propagate without being tagged or captured.

    Examples:
        # Inherit parent context tags
        with posthog.new_context():
            posthog.tag("request_id", "123")
            # Both this event and the exception will be tagged with the context tags
            posthog.capture("event_name", {"property": "value"})
            raise ValueError("Something went wrong")

        # Start with fresh context (no inherited tags)
        with posthog.new_context(fresh=True):
            posthog.tag("request_id", "123")
            # Both this event and the exception will be tagged with the context tags
            posthog.capture("event_name", {"property": "value"})
            raise ValueError("Something went wrong")

    """
    from posthog import capture_exception

    current_tags = _get_current_context().copy()
    current_stack = _context_stack.get()
    new_stack = current_stack + [{}] if fresh else current_stack + [current_tags]
    token = _context_stack.set(new_stack)

    try:
        yield
    except Exception as e:
        if capture_exceptions:
            capture_exception(e)
        raise
    finally:
        _context_stack.reset(token)


def tag(key: str, value: Any) -> None:
    """
    Add a tag to the current context.

    Args:
        key: The tag key
        value: The tag value

    Example:
        posthog.tag("user_id", "123")
    """
    _get_current_context()[key] = value


def get_tags() -> Dict[str, Any]:
    """
    Get all tags from the current context. Note, modifying
    the returned dictionary will not affect the current context.

    Returns:
        Dict of all tags in the current context
    """
    return _get_current_context().copy()


def clear_tags() -> None:
    """Clear all tags in the current context."""
    _get_current_context().clear()


F = TypeVar("F", bound=Callable[..., Any])


def scoped(fresh=False, capture_exceptions=True):
    """
    Decorator that creates a new context for the function. Simply wraps
    the function in a with posthog.new_context(): block.

    Args:
        fresh: Whether to start with a fresh context (default: False)
        capture_exceptions: Whether to capture and track exceptions with posthog error tracking (default: True)

    Example:
        @posthog.scoped()
        def process_payment(payment_id):
            posthog.tag("payment_id", payment_id)
            posthog.tag("payment_method", "credit_card")

            # This event will be captured with tags
            posthog.capture("payment_started")
            # If this raises an exception, it will be captured with tags
            # and then re-raised
            some_risky_function()
    """

    def decorator(func: F) -> F:
        from functools import wraps

        @wraps(func)
        def wrapper(*args, **kwargs):
            with new_context(fresh=fresh, capture_exceptions=capture_exceptions):
                return func(*args, **kwargs)

        return cast(F, wrapper)

    return decorator
