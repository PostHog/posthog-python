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


def test_code_variables_too_long_string_value_replaced(tmpdir):
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
        short_value = "I am short"
        long_value = "x" * 20000
        long_blob = "password_" + "a" * 20000

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

    assert "'short_value': 'I am short'" in output

    assert "$$_posthog_value_too_long_$$" in output

    assert "'long_blob': '$$_posthog_value_too_long_$$'" in output


def test_code_variables_too_long_string_in_nested_dict(tmpdir):
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
        my_data = {
            "short_key": "short_val",
            "long_key": "y" * 20000,
            "nested": {
                "deep_long": "z" * 20000,
                "deep_short": "ok",
            },
        }

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

    assert "short_val" in output
    assert "ok" in output

    assert "$$_posthog_value_too_long_$$" in output
    assert "y" * 1000 not in output
    assert "z" * 1000 not in output


def test_mask_sensitive_data_too_long_dict_key():
    from posthog.exception_utils import (
        CODE_VARIABLES_TOO_LONG_VALUE,
        _compile_patterns,
        _mask_sensitive_data,
    )

    compiled_mask = _compile_patterns([r"(?i)password"])

    result = _mask_sensitive_data(
        {
            "short": "visible",
            "k" * 20000: "hidden_val",
            "password": "secret",
        },
        compiled_mask,
    )

    assert result["short"] == "visible"
    # This then gets shortened by the JSON truncation at 1024 chars anyways so no worries
    assert result["k" * 20000] == CODE_VARIABLES_TOO_LONG_VALUE
    assert result["password"] == "$$_posthog_redacted_based_on_masking_rules_$$"


def test_mask_sensitive_data_circular_ref():
    from posthog.exception_utils import _compile_patterns, _mask_sensitive_data

    compiled_mask = _compile_patterns([r"(?i)password"])

    # Circular dict
    circular_dict = {"key": "value"}
    circular_dict["self"] = circular_dict

    result = _mask_sensitive_data(circular_dict, compiled_mask)
    assert result["key"] == "value"
    assert result["self"] == "<circular ref>"

    # Circular list
    circular_list = ["item"]
    circular_list.append(circular_list)

    result = _mask_sensitive_data(circular_list, compiled_mask)
    assert result[0] == "item"
    assert result[1] == "<circular ref>"


def test_compile_patterns_fast_path_and_regex_fallback():
    from posthog.exception_utils import _compile_patterns, _pattern_matches

    # Simple case-insensitive patterns should become substrings
    simple_only = _compile_patterns([r"(?i)password", r"(?i)token", r"(?i)jwt"])
    substrings, regexes = simple_only
    assert substrings == ["password", "token", "jwt"]
    assert regexes == []

    assert _pattern_matches("my_password_var", simple_only) is True
    assert _pattern_matches("MY_TOKEN", simple_only) is True
    assert _pattern_matches("safe_variable", simple_only) is False

    # Complex regex patterns should stay as compiled regexes
    complex_only = _compile_patterns([r"^__.*", r"\d{3,}", r"^sk_live_"])
    substrings, regexes = complex_only
    assert substrings == []
    assert len(regexes) == 3

    assert _pattern_matches("__dunder", complex_only) is True
    assert _pattern_matches("has_999_numbers", complex_only) is True
    assert _pattern_matches("sk_live_abc123", complex_only) is True
    assert _pattern_matches("normal_var", complex_only) is False

    # Mixed: simple substrings + complex regexes together
    mixed = _compile_patterns(
        [
            r"(?i)secret",  # simple
            r"(?i)api_key",  # simple
            r"^__.*",  # regex
            r"\btoken_\w+",  # regex
        ]
    )
    substrings, regexes = mixed
    assert substrings == ["secret", "api_key"]
    assert len(regexes) == 2

    # Substring matches
    assert _pattern_matches("my_secret", mixed) is True
    assert _pattern_matches("API_KEY_VALUE", mixed) is True

    # Regex matches
    assert _pattern_matches("__private", mixed) is True
    assert _pattern_matches("token_abc", mixed) is True

    # No match
    assert _pattern_matches("safe_var", mixed) is False


def test_mask_sensitive_data_large_dict_replaced():
    from posthog.exception_utils import (
        CODE_VARIABLES_TOO_LONG_VALUE,
        _compile_patterns,
        _mask_sensitive_data,
    )

    compiled_mask = _compile_patterns([r"(?i)password"])

    large_dict = {f"key_{i}": f"value_{i}" for i in range(300)}

    result = _mask_sensitive_data(large_dict, compiled_mask)

    assert result == CODE_VARIABLES_TOO_LONG_VALUE


def test_mask_sensitive_data_large_list_replaced():
    from posthog.exception_utils import (
        CODE_VARIABLES_TOO_LONG_VALUE,
        _compile_patterns,
        _mask_sensitive_data,
    )

    compiled_mask = _compile_patterns([r"(?i)password"])

    large_list = [f"item_{i}" for i in range(300)]

    result = _mask_sensitive_data(large_list, compiled_mask)

    assert result == CODE_VARIABLES_TOO_LONG_VALUE


def test_mask_sensitive_data_large_tuple_replaced():
    from posthog.exception_utils import (
        CODE_VARIABLES_TOO_LONG_VALUE,
        _compile_patterns,
        _mask_sensitive_data,
    )

    compiled_mask = _compile_patterns([r"(?i)password"])

    large_tuple = tuple(f"item_{i}" for i in range(300))

    result = _mask_sensitive_data(large_tuple, compiled_mask)

    assert result == CODE_VARIABLES_TOO_LONG_VALUE
