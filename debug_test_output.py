#!/usr/bin/env python3

import os
import subprocess
import sys
from textwrap import dedent

# Create test script content
app_content = dedent("""
from posthog import Posthog, local_vars_include
from posthog.local_vars import get_code_variables_include

posthog = Posthog('phc_x', host='https://eu.i.posthog.com', 
                  enable_exception_autocapture=True, 
                  enable_code_variables_capture=True,
                  debug=True, 
                  on_error=lambda e, batch: print('error handling batch: ', e, batch))

def throws_error():
    user_id = "test_user_123"
    request_data = {"action": "login", "timestamp": "2023-10-30"}
    error_count = 42
    print(f"Inside throws_error, context state: {get_code_variables_include()}")
    raise ValueError("Something went wrong in throws_error")

@local_vars_include
def decorated_function():
    print(f"Context variable state: {get_code_variables_include()}")
    return throws_error()

decorated_function()
""")

# Write to temporary file
with open("temp_test.py", "w") as f:
    f.write(app_content)

# Set PYTHONPATH and run
env = os.environ.copy()
env['PYTHONPATH'] = os.path.dirname(__file__)

try:
    result = subprocess.run([sys.executable, "temp_test.py"], 
                          capture_output=True, text=True, env=env)
    
    print("STDOUT:")
    print("="*50)
    print(result.stdout)
    print("\nSTDERR:")  
    print("="*50)
    print(result.stderr)
    print("\nReturn code:", result.returncode)
    
except Exception as e:
    print(f"Error running subprocess: {e}")

# Clean up
if os.path.exists("temp_test.py"):
    os.remove("temp_test.py")
