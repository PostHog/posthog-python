import threading
from contextlib import contextmanager
from typing import Any, Dict, Callable, TypeVar, cast

_scopes_local = threading.local()

def _init_guard() -> None:
    if not hasattr(_scopes_local, "context_stack"):
        _scopes_local.context_stack = [{}]


def _get_current_context() -> Dict[str, Any]:
    _init_guard()
    return _scopes_local.context_stack[-1]


@contextmanager
def new_context():
    # TODO - we could extend this context idea to also apply to other event types eventually,
    # but right now it only applies to exceptions...
    """
    Create a new context scope that will be active for the duration of the with block.
    Any tags set within this scope will be isolated to this context. Tags added to a
    context will be added to exceptions captured within that context.

    NOTE: tags set within a context will only be added to exceptions captured within that
    context - ensure you call `posthog.capture_exception()` before the end of the with
    block, or the extra tags will be lost.

    It's strongly recommended to use the `posthog.tracked` decorator to instrument functions, rather
    than directly using this context manager.

    Example:
        with posthog.new_context():
            posthog.tag("user_id", "123")
            try:
                # Do something that might raise an exception
            except Exception as e:
                posthog.capture_exception(e)
                raise e
    """
    _init_guard()
    _scopes_local.context_stack.append({})
    try:
        yield
    finally:
        if len(_scopes_local.context_stack) > 1:
            _scopes_local.context_stack.pop()


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


F = TypeVar('F', bound=Callable[..., Any])
def tracked(func: F) -> F:
    """
    Decorator that creates a new context for the function, wraps the function in a
    try/except block, and if an exception occurs, captures it with the current context
    tags before re-raising it. This is the recommended way to wrap/track functions for
    posthog error tracking.

    Args:
        func: The function to wrap

    Example:
        @posthog.tracked
        def process_payment(payment_id):
            posthog.tag("payment_id", payment_id)
            posthog.tag("payment_method", "credit_card")
            # If this raises an exception, it will be captured with tags
            # and then re-raised
    """
    from functools import wraps
    import posthog

    @wraps(func)
    def wrapper(*args, **kwargs):
        with new_context():
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Capture the exception with current context tags
                # The capture_exception function will handle deduplication
                posthog.capture_exception(e, properties=get_tags())
                raise  # Re-raise the exception after capturing it

    return cast(F, wrapper)
