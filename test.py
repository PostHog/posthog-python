from contextvars import ContextVar
import posthog
from posthog.local_vars import get_code_variables_include
import threading
import time

posthog.api_key = "phc_J1o2BXYxzXBHJeG2mS5hk62ijkTWk38Z385lO0MhU5w"
posthog.host = "http://localhost:8010" 
posthog.debug = True
posthog.enable_exception_autocapture = True
posthog.enable_code_variables_capture = True

var = ContextVar[str]("var", default="unknown")

@posthog.include
def with_include():
    simple_number = 5
    simple_string = "hello"
    simple_list = [1, 2, 3]
    simple_dict = {"a": 1, "b": 2, "c": 3}
    simple_tuple = (1, 2, 3)
    simple_set = {1, 2, 3}
    simple_bool = True
    simple_none = None
    simple_float = 1.0
    complex_object_instance = object()

    try:
        raise Exception("test exception")
    except Exception as e:
        posthog.capture_exception(e)

@posthog.ignore
def with_ignore():
    simple_number = 5
    simple_string = "hello"
    simple_list = [1, 2, 3]
    simple_dict = {"a": 1, "b": 2, "c": 3}
    simple_tuple = (1, 2, 3)
    simple_set = {1, 2, 3}
    simple_bool = True
    simple_none = None
    simple_float = 1.0

    raise Exception("test exception")

with_include()