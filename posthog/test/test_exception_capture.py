import pytest
import sys
import subprocess

from textwrap import dedent


def test_excepthook(tmpdir):
    app = tmpdir.join("app.py")
    app.write(
        dedent(
            """
    from posthog import Posthog
    posthog = Posthog('phc_x', host='http://localhost:8000', enable_exception_autocapture=True, debug=True, on_error=lambda e, batch: print('error handling batch: ', e, batch))

    # frame_value = "LOL"

    1/0
    """
        )
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output([sys.executable, str(app)], stderr=subprocess.STDOUT)

    output = excinfo.value.output
    print(output)

    assert b"ZeroDivisionError" in output
    assert b"LOL" in output
    assert b"DEBUG:posthog:data uploaded successfully" in output
    assert b'"$exception_list": [{"mechanism": {"type": "generic", "handled": true}, "module": null, "type": "ZeroDivisionError", "value": "division by zero", "stacktrace": {"frames": [{"filename": "app.py", "abs_path"' in output
