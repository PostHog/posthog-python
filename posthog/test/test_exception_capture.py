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


def test_code_variables_capture(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    import os
    from posthog import Posthog
    
    class UnserializableObject:
        pass
    
    posthog = Posthog(
        'phc_x', 
        host='https://eu.i.posthog.com', 
        debug=True, 
        enable_exception_autocapture=True,
        capture_exception_code_variables=True,
        project_root=os.path.dirname(os.path.abspath(__file__))
    )
    
    def trigger_error():
        my_string = "hello world"
        my_number = 42
        my_bool = True
        my_dict = {"name": "test", "value": 123}
        my_sensitive_dict = {
            "safe_key": "safe_value",
            "password": "secret123",  # key matches pattern -> should be masked
            "other_key": "contains_password_here",  # value matches pattern -> should be masked
        }
        my_nested_dict = {
            "level1": {
                "level2": {
                    "api_key": "nested_secret",  # deeply nested key matches
                    "data": "contains_token_here",  # deeply nested value matches
                    "safe": "visible",
                }
            }
        }
        my_list = ["safe_item", "has_password_inside", "another_safe"]
        my_tuple = ("tuple_safe", "secret_in_value", "tuple_also_safe")
        my_list_of_dicts = [
            {"id": 1, "password": "list_dict_secret"},
            {"id": 2, "value": "safe_value"},
        ]
        my_obj = UnserializableObject()
        my_password = "secret123"  # Should be masked by default (name matches)
        my_innocent_var = "contains_password_here"  # Should be masked by default (value matches)
        __should_be_ignored = "hidden"  # Should be ignored by default
        
        1/0  # Trigger exception
    
    def intermediate_function():
        request_id = "abc-123"
        user_count = 100
        is_active = True
        
        trigger_error()
    
    def process_data():
        batch_size = 50
        retry_count = 3
        
        intermediate_function()
    
    process_data()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output

    assert b"ZeroDivisionError" in output
    assert b"code_variables" in output

    # Variables from trigger_error frame
    assert b"'my_string': 'hello world'" in output
    assert b"'my_number': 42" in output
    assert b"'my_bool': 'True'" in output
    assert b'"my_dict": "{\\"name\\": \\"test\\", \\"value\\": 123}"' in output
    assert (
        b'{\\"safe_key\\": \\"safe_value\\", \\"password\\": \\"$$_posthog_redacted_based_on_masking_rules_$$\\", \\"other_key\\": \\"$$_posthog_redacted_based_on_masking_rules_$$\\"}'
        in output
    )
    assert (
        b'{\\"level1\\": {\\"level2\\": {\\"api_key\\": \\"$$_posthog_redacted_based_on_masking_rules_$$\\", \\"data\\": \\"$$_posthog_redacted_based_on_masking_rules_$$\\", \\"safe\\": \\"visible\\"}}}'
        in output
    )
    assert (
        b'[\\"safe_item\\", \\"$$_posthog_redacted_based_on_masking_rules_$$\\", \\"another_safe\\"]'
        in output
    )
    assert (
        b'[\\"tuple_safe\\", \\"$$_posthog_redacted_based_on_masking_rules_$$\\", \\"tuple_also_safe\\"]'
        in output
    )
    assert (
        b'[{\\"id\\": 1, \\"password\\": \\"$$_posthog_redacted_based_on_masking_rules_$$\\"}, {\\"id\\": 2, \\"value\\": \\"safe_value\\"}]'
        in output
    )
    assert b"<__main__.UnserializableObject object at" in output
    assert b"'my_password': '$$_posthog_redacted_based_on_masking_rules_$$'" in output
    assert (
        b"'my_innocent_var': '$$_posthog_redacted_based_on_masking_rules_$$'" in output
    )
    assert b"'__should_be_ignored':" not in output

    # Variables from intermediate_function frame
    assert b"'request_id': 'abc-123'" in output
    assert b"'user_count': 100" in output
    assert b"'is_active': 'True'" in output

    # Variables from process_data frame
    assert b"'batch_size': 50" in output
    assert b"'retry_count': 3" in output


def test_code_variables_context_override(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    import os
    import posthog
    from posthog import Posthog
    
    posthog_client = Posthog(
        'phc_x', 
        host='https://eu.i.posthog.com', 
        debug=True, 
        enable_exception_autocapture=True,
        capture_exception_code_variables=False,
        project_root=os.path.dirname(os.path.abspath(__file__))
    )
    
    def process_data():
        bank = "should_be_masked"
        __dunder_var = "should_be_visible"
        
        1/0
    
    with posthog.new_context(client=posthog_client):
        posthog.set_capture_exception_code_variables_context(True)
        posthog.set_code_variables_mask_patterns_context([r"(?i).*bank.*"])
        posthog.set_code_variables_ignore_patterns_context([])
        
        process_data()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output

    assert b"ZeroDivisionError" in output
    assert b"code_variables" in output
    assert b"'bank': '$$_posthog_redacted_based_on_masking_rules_$$'" in output
    assert b"'__dunder_var': 'should_be_visible'" in output


def test_code_variables_size_limiter(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    import os
    from posthog import Posthog
    
    posthog = Posthog(
        'phc_x', 
        host='https://eu.i.posthog.com', 
        debug=True, 
        enable_exception_autocapture=True,
        capture_exception_code_variables=True,
        project_root=os.path.dirname(os.path.abspath(__file__))
    )
    
    def trigger_error():
        var_a = "a" * 2000
        var_b = "b" * 2000
        var_c = "c" * 2000
        var_d = "d" * 2000
        var_e = "e" * 2000
        var_f = "f" * 2000
        var_g = "g" * 2000
        
        1/0
    
    def intermediate_function():
        var_h = "h" * 2000
        var_i = "i" * 2000
        var_j = "j" * 2000
        var_k = "k" * 2000
        var_l = "l" * 2000
        var_m = "m" * 2000
        var_n = "n" * 2000
        
        trigger_error()
    
    def process_data():
        var_o = "o" * 2000
        var_p = "p" * 2000
        var_q = "q" * 2000
        var_r = "r" * 2000
        var_s = "s" * 2000
        var_t = "t" * 2000
        var_u = "u" * 2000
        
        intermediate_function()
    
    process_data()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output.decode("utf-8")

    assert "ZeroDivisionError" in output
    assert "code_variables" in output

    captured_vars = []
    for var_name in [
        "var_a",
        "var_b",
        "var_c",
        "var_d",
        "var_e",
        "var_f",
        "var_g",
        "var_h",
        "var_i",
        "var_j",
        "var_k",
        "var_l",
        "var_m",
        "var_n",
        "var_o",
        "var_p",
        "var_q",
        "var_r",
        "var_s",
        "var_t",
        "var_u",
    ]:
        if f"'{var_name}'" in output:
            captured_vars.append(var_name)

    assert len(captured_vars) > 0
    assert len(captured_vars) < 21


def test_code_variables_disabled_capture(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    import os
    from posthog import Posthog
    
    posthog = Posthog(
        'phc_x', 
        host='https://eu.i.posthog.com', 
        debug=True, 
        enable_exception_autocapture=True,
        capture_exception_code_variables=False,
        project_root=os.path.dirname(os.path.abspath(__file__))
    )
    
    def trigger_error():
        my_string = "hello world"
        my_number = 42
        my_bool = True
        
        1/0
    
    trigger_error()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output.decode("utf-8")

    assert "ZeroDivisionError" in output
    assert "'code_variables':" not in output
    assert '"code_variables":' not in output
    assert "'my_string'" not in output
    assert "'my_number'" not in output


def test_code_variables_enabled_then_disabled_in_context(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    import os
    import posthog
    from posthog import Posthog
    
    posthog_client = Posthog(
        'phc_x', 
        host='https://eu.i.posthog.com', 
        debug=True, 
        enable_exception_autocapture=True,
        capture_exception_code_variables=True,
        project_root=os.path.dirname(os.path.abspath(__file__))
    )
    
    def process_data():
        my_var = "should not be captured"
        important_value = 123
        
        1/0
    
    with posthog.new_context(client=posthog_client):
        posthog.set_capture_exception_code_variables_context(False)
        
        process_data()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output.decode("utf-8")

    assert "ZeroDivisionError" in output
    assert "'code_variables':" not in output
    assert '"code_variables":' not in output
    assert "'my_var'" not in output
    assert "'important_value'" not in output


def test_code_variables_repr_fallback(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    import os
    import re
    from datetime import datetime, timedelta
    from decimal import Decimal
    from fractions import Fraction
    from posthog import Posthog
    
    class CustomReprClass:
        def __repr__(self):
            return '<CustomReprClass: custom representation>'
    
    posthog = Posthog(
        'phc_x', 
        host='https://eu.i.posthog.com', 
        debug=True, 
        enable_exception_autocapture=True,
        capture_exception_code_variables=True,
        project_root=os.path.dirname(os.path.abspath(__file__))
    )
    
    def trigger_error():
        my_regex = re.compile(r'\\d+')
        my_datetime = datetime(2024, 1, 15, 10, 30, 45)
        my_timedelta = timedelta(days=5, hours=3)
        my_decimal = Decimal('123.456')
        my_fraction = Fraction(3, 4)
        my_set = {1, 2, 3}
        my_frozenset = frozenset([4, 5, 6])
        my_bytes = b'hello bytes'
        my_bytearray = bytearray(b'mutable bytes')
        my_memoryview = memoryview(b'memory view')
        my_complex = complex(3, 4)
        my_range = range(10)
        my_custom = CustomReprClass()
        my_lambda = lambda x: x * 2
        my_function = trigger_error
        
        1/0
    
    trigger_error()
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output.decode("utf-8")

    assert "ZeroDivisionError" in output
    assert "code_variables" in output

    assert "re.compile(" in output and "\\\\d+" in output
    assert "datetime.datetime(2024, 1, 15, 10, 30, 45)" in output
    assert "datetime.timedelta(days=5, seconds=10800)" in output
    assert "Decimal('123.456')" in output
    assert "Fraction(3, 4)" in output
    assert "{1, 2, 3}" in output
    assert "frozenset({4, 5, 6})" in output
    assert "b'hello bytes'" in output
    assert "bytearray(b'mutable bytes')" in output
    assert "<memory at" in output
    assert "(3+4j)" in output
    assert "range(0, 10)" in output
    assert "<CustomReprClass: custom representation>" in output
    assert "<lambda>" in output
    assert "<function trigger_error at" in output
