import contextvars
from contextlib import contextmanager
from typing import Any, Callable, Dict, TypeVar, cast
from posthog import capture_exception

_context_stack: contextvars.ContextVar[list] = contextvars.ContextVar("posthog_context_stack", default=[{}])


def _get_current_context() -> Dict[str, Any]:
    return _context_stack.get()[-1]


@contextmanager
def new_context():
    # TODO - we could extend this context idea to also apply to other event types eventually,
    # but right now it only applies to exceptions...
    """
    Create a new context scope that will be active for the duration of the with block.
    Any tags set within this scope will be isolated to this context. Any exceptions raised
    within the context will be captured and tagged with the context tags.

    Example:
        with posthog.new_context():
            posthog.tag("user_id", "123")
            # The exception will be captured and tagged with the context tags
            raise ValueError("Something went wrong")

    """
    current_stack = _context_stack.get()
    new_stack = current_stack + [{}]
    token = _context_stack.set(new_stack)
    try:
        yield
    except Exception as e:
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


def scoped(func: F) -> F:
    """
    Decorator that creates a new context for the function, wraps the function in a
    try/except block, and if an exception occurs, captures it with the current context
    tags before re-raising it.

    Args:
        func: The function to wrap

    Example:
        @posthog.scoped
        def process_payment(payment_id):
            posthog.tag("payment_id", payment_id)
            posthog.tag("payment_method", "credit_card")
            # If this raises an exception, it will be captured with tags
            # and then re-raised
    """
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        with new_context():
            return func(*args, **kwargs)


    return cast(F, wrapper)
