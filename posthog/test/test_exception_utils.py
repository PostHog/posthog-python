import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from posthog import exception_utils
from posthog.exception_utils import format_timestamp


def test_format_timestamp_converts_aware_value_to_utc():
    value = datetime(
        2026,
        6,
        27,
        17,
        45,
        0,
        123456,
        tzinfo=timezone(timedelta(hours=5, minutes=45)),
    )

    assert format_timestamp(value) == "2026-06-27T12:00:00.123456Z"


# Source context, traversal and frame classification are observable payload contracts.
def test_source_context_keeps_file_order_and_five_lines_on_each_side(tmp_path):
    source = tmp_path / "application.py"
    source.write_text("\n".join(f"line {n}" for n in range(13)))

    assert exception_utils.get_lines_from_file(str(source), 6) == (
        ["line 1", "line 2", "line 3", "line 4", "line 5"],
        "line 6",
        ["line 7", "line 8", "line 9", "line 10", "line 11"],
    )


def test_source_context_prefers_loader_over_file(tmp_path):
    source = tmp_path / "application.py"
    source.write_text("stale contents")
    loader = SimpleNamespace(get_source=lambda module: "before\r\ncurrent\r\nafter")

    assert exception_utils.get_lines_from_file(
        str(source), 1, loader=loader, module="app"
    ) == (
        ["before"],
        "current",
        ["after"],
    )


@pytest.mark.parametrize("failure", [ImportError, OSError])
def test_source_context_falls_back_when_loader_cannot_read_source(tmp_path, failure):
    def unavailable(module):
        raise failure("source unavailable")

    source = tmp_path / "application.py"
    source.write_text("current\nafter\n")
    assert exception_utils.get_lines_from_file(
        str(source), 0, loader=SimpleNamespace(get_source=unavailable), module="app"
    ) == ([], "current", ["after"])


def test_source_context_handles_changed_files(tmp_path):
    source = tmp_path / "application.py"
    source.write_text("current\n")
    assert exception_utils.get_lines_from_file(str(source), 99) == ([], None, [])


def test_source_context_does_not_replace_empty_loader_source_with_stale_file(tmp_path):
    source = tmp_path / "application.py"
    source.write_text("stale contents")
    loader = SimpleNamespace(get_source=lambda module: "")
    assert exception_utils.get_lines_from_file(str(source), 0, loader=loader) == (
        [],
        None,
        [],
    )


def test_source_context_handles_unreadable_file(monkeypatch):
    def unavailable(filename):
        raise OSError("source unavailable")

    monkeypatch.setattr(exception_utils.linecache, "getlines", unavailable)
    assert exception_utils.get_lines_from_file("application.py", 0) == ([], None, [])


@pytest.mark.parametrize("relationship", ["cause", "context"])
def test_walk_exception_chain_is_outermost_first_and_stops_at_cycles(relationship):
    outer, inner = RuntimeError("outer"), ValueError("inner")
    if relationship == "cause":
        outer.__cause__ = inner
        inner.__cause__ = outer
    else:
        outer.__context__ = inner
        inner.__context__ = outer

    assert list(exception_utils.walk_exception_chain((type(outer), outer, None))) == [
        (RuntimeError, outer, None),
        (ValueError, inner, None),
    ]


def test_walk_exception_chain_stops_at_suppressed_context():
    outer = RuntimeError("outer")
    outer.__context__ = ValueError("ignored")
    outer.__suppress_context__ = True
    assert list(exception_utils.walk_exception_chain((RuntimeError, outer, None))) == [
        (RuntimeError, outer, None)
    ]
    assert list(exception_utils.walk_exception_chain((None, None, None))) == []


def test_event_frame_iteration_keeps_top_level_thread_and_exception_order():
    first, second, third = (
        {"function": "main"},
        {"function": "thread"},
        {"function": "fail"},
    )
    event = {
        "stacktrace": {"frames": [first]},
        "threads": {"values": [{}, {"stacktrace": {"frames": [second]}}]},
        "exception": {
            "values": [{"stacktrace": {}}, {}, {"stacktrace": {"frames": [third]}}]
        },
    }
    assert list(exception_utils.iter_event_frames(event)) == [first, second, third]
    assert (
        list(exception_utils.iter_event_frames({"threads": {}, "exception": {}})) == []
    )


@pytest.mark.parametrize(
    "frame, expected",
    [
        ({"in_app": False, "module": "app"}, False),
        ({"module": "app.handlers"}, True),
        ({"module": "app2.handlers"}, None),
        ({"module": "vendor.plugin"}, False),
        ({"abs_path": "/venv/site-packages/pkg.py"}, False),
        ({"abs_path": "/venv/dist-packages/pkg.py"}, False),
        ({"abs_path": "/project/app.py"}, True),
        ({"abs_path": "/elsewhere/app.py"}, None),
        ({}, None),
    ],
)
def test_frame_classification_preserves_overrides_and_module_boundaries(
    frame, expected
):
    frames = [frame]
    assert (
        exception_utils.set_in_app_in_frames(
            frames,
            in_app_exclude=["vendor", "app"],
            in_app_include=["app"],
            project_root="/project/",
        )
        is frames
    )
    assert frame.get("in_app") is expected


def test_frame_classification_accepts_missing_options_and_empty_frames():
    frame = {"module": "app", "abs_path": "/project/app.py"}
    exception_utils.set_in_app_in_frames([frame], None, None)
    assert "in_app" not in frame
    assert exception_utils.set_in_app_in_frames([], None, None) is None


def test_exc_info_preserves_explicit_tuple():
    error = ValueError("reported")
    assert exception_utils.exc_info_from_error((ValueError, error, None)) == (
        ValueError,
        error,
        None,
    )


def test_exc_info_preserves_active_traceback_when_synthesis_fails(monkeypatch):
    def unavailable(error):
        raise RuntimeError("cannot synthesize traceback")

    monkeypatch.setattr(exception_utils, "construct_artificial_traceback", unavailable)
    error = ValueError("active")
    try:
        raise error
    except ValueError:
        expected = sys.exc_info()
        assert exception_utils.exc_info_from_error(error) == expected


def test_exc_info_does_not_borrow_an_unrelated_active_exception(monkeypatch):
    monkeypatch.setattr(
        exception_utils, "construct_artificial_traceback", lambda error: None
    )
    error = ValueError("reported")
    try:
        raise RuntimeError("unrelated")
    except RuntimeError:
        assert exception_utils.exc_info_from_error(error) == (ValueError, error, None)
    assert exception_utils.exc_info_from_error(error) == (ValueError, error, None)


@pytest.mark.parametrize("invalid", [None, "error", (ValueError, ValueError("bad"))])
def test_exc_info_rejects_non_exception_inputs(invalid):
    with pytest.raises(ValueError, match="Expected Exception object"):
        exception_utils.exc_info_from_error(invalid)


@pytest.mark.parametrize(
    "mechanism, expected",
    [
        (
            {
                "type": "custom",
                "handled": False,
                "source": "cause",
                "synthetic": False,
                "extra": "kept",
            },
            {
                "type": "custom",
                "handled": False,
                "source": "cause",
                "synthetic": False,
                "extra": "kept",
            },
        ),
        (
            {
                "type": "",
                "handled": 1,
                "source": False,
                "synthetic": "yes",
                "extra": "kept",
            },
            {"synthetic": False, "extra": "kept"},
        ),
        (None, {"synthetic": False}),
    ],
)
def test_exception_serialization_validates_mechanism_fields_independently(
    mechanism, expected
):
    error = ValueError("reported")
    result = exception_utils.single_exception_from_error_tuple(
        ValueError, error, None, mechanism=mechanism
    )
    assert result == {
        "mechanism": expected,
        "module": None,
        "type": "ValueError",
        "value": "reported",
    }


def test_exception_tree_truncates_long_cyclic_cause_chain_in_outermost_first_order():
    errors = [ValueError(str(n)) for n in range(60)]
    for outer, inner in zip(errors, errors[1:]):
        outer.__cause__ = inner
    errors[-1].__cause__ = errors[0]
    result = exception_utils.exceptions_from_error_tuple((ValueError, errors[0], None))
    assert [entry["value"] for entry in result] == [str(n) for n in range(50)]
    assert [entry["mechanism"]["exception_id"] for entry in result] == list(range(50))
    assert [entry["mechanism"]["parent_id"] for entry in result[1:]] == list(range(49))
    assert all(entry["mechanism"]["source"] == "cause" for entry in result[1:])


def test_exception_tree_stops_at_a_cycle_before_the_entry_limit():
    outer, inner = ValueError("outer"), RuntimeError("inner")
    outer.__context__ = inner
    inner.__context__ = outer
    result = exception_utils.exceptions_from_error_tuple((ValueError, outer, None))
    assert [entry["value"] for entry in result] == ["outer", "inner"]
    assert result[1]["mechanism"] == {
        "exception_id": 1,
        "parent_id": 0,
        "type": "chained",
        "source": "context",
        "synthetic": False,
    }
