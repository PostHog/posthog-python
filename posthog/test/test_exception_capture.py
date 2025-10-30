import os
import subprocess
import sys
from textwrap import dedent

import pytest


def test_excepthook(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    from posthog import Posthog
    posthog = Posthog('phc_x', host='https://eu.i.posthog.com', enable_exception_autocapture=True, debug=True, on_error=lambda e, batch: print('error handling batch: ', e, batch))

    # frame_value = "LOL"

    1/0
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output

    assert b"ZeroDivisionError" in output
    assert b"LOL" in output
    assert b"DEBUG:posthog:data uploaded successfully" in output
    assert (
        b'"$exception_list": [{"mechanism": {"type": "generic", "handled": true}, "module": null, "type": "ZeroDivisionError", "value": "division by zero", "stacktrace": {"frames": [{"platform": "python", "filename": "app.py", "abs_path"'
        in output
    )


def test_local_vars_with_include_decorator(tmpdir):
    app = tmpdir.join("app_include.py")
    app.write(
        dedent(
            """
    from posthog import Posthog, include
    posthog = Posthog('phc_x', host='https://eu.i.posthog.com', 
                      enable_exception_autocapture=True, 
                      enable_code_variables_capture=True,
                      debug=True, 
                      on_error=lambda e, batch: print('error handling batch: ', e, batch))

    def throws_error():
        user_id = "test_user_123"
        request_data = {"action": "login", "timestamp": "2023-10-30"}
        error_count = 42
        raise ValueError("Something went wrong in throws_error")

    @include
    def decorated_function():
        return throws_error()

    decorated_function()
    """
        )
    )



    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output
    
    # Check that exception was captured
    assert b"ValueError" in output
    assert b"Something went wrong in throws_error" in output
    
    # Check that local variables were captured
    assert b'"$exception_local_vars"' in output
    assert b'"user_id": "test_user_123"' in output
    assert b'"request_data"' in output
    assert b'"error_count": 42' in output


def test_local_vars_with_ignore_decorator(tmpdir):
    app = tmpdir.join("app_ignore.py")
    app.write(
        dedent(
            """
    from posthog import Posthog, ignore
    posthog = Posthog('phc_x', host='https://eu.i.posthog.com', 
                      enable_exception_autocapture=True, 
                      enable_code_variables_capture=True,
                      debug=True, 
                      on_error=lambda e, batch: print('error handling batch: ', e, batch))

    def throws_error():
        secret_password = "super_secret_123"
        api_key = "sk_1234567890abcdef"
        sensitive_data = {"ssn": "123-45-6789", "credit_card": "4111-1111-1111-1111"}
        raise ValueError("Something went wrong in throws_error")

    @ignore
    def decorated_function():
        return throws_error()

    decorated_function()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output
    
    assert b"ValueError" in output
    assert b"Something went wrong in throws_error" in output
    
    assert b'"$exception_local_vars"' not in output
