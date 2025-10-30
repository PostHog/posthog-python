from contextvars import ContextVar
import posthog
from posthog.local_vars import get_code_variables_include
import threading
import time

posthog.api_key = "phc_J1o2BXYxzXBHJeG2mS5hk62ijkTWk38Z385lO0MhU5w"
posthog.host = "http://localhost:8010" 
posthog.debug = True
posthog.enable_exception_autocapture = True
posthog.enable_code_variables_autocapture = True

var = ContextVar[str]("var", default="unknown")

@posthog.include
def function_A():
    """Function A: set posthog_include, print it, wait 2 seconds, print it again"""
    print("Function A - Initial state:", get_code_variables_include())
    time.sleep(2)
    print("Function A - After 2 seconds:", get_code_variables_include())

@posthog.ignore
def function_B():
    """Function B: set posthog_ignore"""
    print("Function B - State:", get_code_variables_include())

def run_concurrent_functions():
    """Run functions A and B simultaneously using threading"""
    # Create threads for both functions
    thread_a = threading.Thread(target=function_A, name="ThreadA")
    thread_b = threading.Thread(target=function_B, name="ThreadB")
    
    print("Starting both functions concurrently...")
    
    # Start both threads
    thread_a.start()
    thread_b.start()
    
    # Wait for both threads to complete
    thread_a.join()
    thread_b.join()
    
    print("Both functions completed!")

# Original functions (keeping for reference)
@posthog.include
def outer_function():
    print("Original outer function:", get_code_variables_include())
    inner_function()
    print("Original outer function after inner:", get_code_variables_include())

@posthog.ignore
def inner_function():
    print("Original inner function:", get_code_variables_include())

if __name__ == "__main__":
    print("=== Running concurrent functions example ===")
    run_concurrent_functions()
    
    print("\n=== Running original example ===")
    outer_function()