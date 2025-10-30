from contextvars import ContextVar
import posthog
from posthog.local_vars import get_code_variables_include

posthog.api_key = "phc_J1o2BXYxzXBHJeG2mS5hk62ijkTWk38Z385lO0MhU5w"
posthog.host = "http://localhost:8010" 
posthog.debug = True
posthog.enable_exception_autocapture = True
posthog.enable_code_variables_autocapture = True

var = ContextVar[str]("var", default="unknown")

@posthog.include
def outer_function():
  print(get_code_variables_include())
  inner_function()
  print(get_code_variables_include())

@posthog.ignore
def inner_function():
  print(get_code_variables_include())

outer_function()