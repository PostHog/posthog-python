"""
Local variables capture for PostHog exception tracking.

This module provides decorators and context management for capturing local variables
when exceptions occur, using Python's contextvars mechanism.
"""

import contextvars
from functools import wraps
from typing import Any, Callable, TypeVar

# ContextVar for local variables capture in exception tracking
_code_variables_include: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "posthog_code_variables_include", default=False
)

F = TypeVar('F', bound=Callable[..., Any])


def get_code_variables_include() -> bool:
    """Get the current state of local variables capture for exception tracking."""
    return _code_variables_include.get()


def set_code_variables_include(enabled: bool) -> None:
    """Set the state of local variables capture for exception tracking."""
    _code_variables_include.set(enabled)


def include(func: F) -> F:
    """
    Decorator to enable local variables capture for exceptions in this function.
    
    When an exception occurs within a function decorated with @include,
    local variables from the frame where the exception originated will be captured
    and attached to the exception data.
    
    Examples:
        ```python
        from posthog import include
        
        @include
        def risky_function():
            user_id = "12345"
            data = {"key": "value"}
            # If an exception occurs here, user_id and data will be captured
            raise ValueError("Something went wrong")
        ```
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Set the context variable to True for this function execution
        token = _code_variables_include.set(True)
        try:
            return func(*args, **kwargs)
        finally:
            # Reset the context variable when function exits
            _code_variables_include.reset(token)
    
    return wrapper  # type: ignore


def ignore(func: F) -> F:
    """
    Decorator to disable local variables capture for exceptions in this function.
    
    When an exception occurs within a function decorated with @ignore,
    local variables will not be captured even if a parent function has @include.
    
    Examples:
        ```python
        from posthog import ignore
        
        @ignore  
        def sensitive_function():
            password = "secret123"
            # If an exception occurs here, password will NOT be captured
            raise ValueError("Something went wrong")
        ```
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Set the context variable to False for this function execution
        token = _code_variables_include.set(False)
        try:
            return func(*args, **kwargs)
        finally:
            # Reset the context variable when function exits
            _code_variables_include.reset(token)
    
    return wrapper  # type: ignore
