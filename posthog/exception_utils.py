# Portions of this file are derived from getsentry/sentry-python
# Copyright (c) 2018 Functional Software, Inc. dba Sentry
# Licensed under the MIT License: https://github.com/getsentry/sentry-python/blob/master/LICENSE

# 💖open source (under MIT License)
# We want to keep payloads as similar to Sentry as possible for easy interoperability

import dataclasses
import functools
import json
import linecache
import math
import os
import re
import sys
import types
from datetime import datetime
from types import FrameType, TracebackType  # noqa: F401
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Pattern,
    Set,
    Tuple,
    TypedDict,
    TypeVar,
    Union,
    cast,
)

from posthog.args import ExceptionArg, ExcInfo  # noqa: F401

try:
    # Python 3.11
    from builtins import BaseExceptionGroup
except ImportError:
    # Python 3.10 and below
    BaseExceptionGroup = None  # type: ignore


DEFAULT_MAX_VALUE_LENGTH = 1024

DEFAULT_CODE_VARIABLES_MASK_PATTERNS = [
    r"(?i)password",
    r"(?i)secret",
    r"(?i)passwd",
    r"(?i)pwd",
    r"(?i)api_key",
    r"(?i)apikey",
    r"(?i)auth",
    r"(?i)credentials",
    r"(?i)privatekey",
    r"(?i)private_key",
    r"(?i)token",
    r"(?i)aws_access_key_id",
    r"(?i)_pass",
    r"(?i)sk_",
    r"(?i)jwt",
    r"(?i)connection_string",
    r"(?i)connectionstring",
    r"(?i)conn_str",
    r"(?i)connstr",
    r"(?i)dsn",
]

DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS = [r"^__.*"]

DEFAULT_CODE_VARIABLES_MASK_URL_CREDENTIALS = True

CODE_VARIABLES_REDACTED_VALUE = "$$_posthog_redacted_based_on_masking_rules_$$"
CODE_VARIABLES_TOO_LONG_VALUE = "$$_posthog_value_too_long_$$"

_MAX_VALUE_LENGTH_FOR_PATTERN_MATCH = 5_000
_MAX_COLLECTION_ITEMS_TO_SCAN = 100
_REGEX_METACHARACTERS = frozenset(r"\.^$*+?{}[]|()")

# Max recursion depth into nested structures while masking (cycles are guarded separately).
_MAX_MASK_DEPTH = 25

# Cap on total non-scalar nodes traversed per top-level value; the depth/collection caps
# don't bound aggregate work, so this stops a wide-and-deep graph from fanning out.
_MAX_TOTAL_NODES_TO_MASK = 200

# Matches `user:pass` credentials in URLs/DSNs (e.g. `postgresql://user:pass@host`); the
# bounded scheme length avoids catastrophic backtracking.
_URL_CREDENTIALS_RE = re.compile(
    r"([a-z][a-z0-9+.\-]{0,30}://)(?=[^/@\s]*:)[^/\s]*@", re.IGNORECASE
)


def _redact_url_credentials(value):
    if "://" not in value:
        return value
    return _URL_CREDENTIALS_RE.sub(
        r"\g<1>" + CODE_VARIABLES_REDACTED_VALUE + "@", value
    )


DEFAULT_TOTAL_VARIABLES_SIZE_LIMIT = 20 * 1024


class VariableSizeLimiter:
    def __init__(self, max_size=DEFAULT_TOTAL_VARIABLES_SIZE_LIMIT):
        self.max_size = max_size
        self.current_size = 0

    def can_add(self, size):
        return self.current_size + size <= self.max_size

    def add(self, size):
        self.current_size += size

    def get_remaining_space(self):
        return self.max_size - self.current_size


LogLevelStr = Literal["fatal", "critical", "error", "warning", "info", "debug"]

Event = TypedDict(
    "Event",
    {
        "breadcrumbs": Dict[
            Literal["values"], List[Dict[str, Any]]
        ],  # TODO: We can expand on this type
        "check_in_id": str,
        "contexts": Dict[str, Dict[str, object]],
        "dist": str,
        "duration": Optional[float],
        "environment": str,
        "errors": List[Dict[str, Any]],  # TODO: We can expand on this type
        "event_id": str,
        "exception": Dict[
            Literal["values"], List[Dict[str, Any]]
        ],  # TODO: We can expand on this type
        # "extra": MutableMapping[str, object],
        # "fingerprint": List[str],
        "level": LogLevelStr,
        # "logentry": Mapping[str, object],
        "logger": str,
        # "measurements": Dict[str, MeasurementValue],
        "message": str,
        "modules": Dict[str, str],
        # "monitor_config": Mapping[str, object],
        "monitor_slug": Optional[str],
        "platform": Literal["python"],
        "profile": object,
        "release": str,
        "request": Dict[str, object],
        # "sdk": Mapping[str, object],
        "server_name": str,
        "spans": List[Dict[str, object]],
        "stacktrace": Dict[
            str, object
        ],  # We access this key in the code, but I am unsure whether we ever set it
        "start_timestamp": datetime,
        "status": Optional[str],
        # "tags": MutableMapping[
        #     str, str
        # ],  # Tags must be less than 200 characters each
        "threads": Dict[
            Literal["values"], List[Dict[str, Any]]
        ],  # TODO: We can expand on this type
        "timestamp": Optional[datetime],  # Must be set before sending the event
        "transaction": str,
        # "transaction_info": Mapping[str, Any],  # TODO: We can expand on this type
        "type": Literal["check_in", "transaction"],
        "user": Dict[str, object],
        "_metrics_summary": Dict[str, object],
    },
    total=False,
)


epoch = datetime(1970, 1, 1)


BASE64_ALPHABET = re.compile(r"^[a-zA-Z0-9/+=]*$")

SENSITIVE_DATA_SUBSTITUTE = "[Filtered]"


def to_timestamp(value):
    # type: (datetime) -> float
    return (value - epoch).total_seconds()


def format_timestamp(value):
    # type: (datetime) -> str
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_hint_with_exc_info(exc_info=None):
    # type: (Optional[ExcInfo]) -> Dict[str, Optional[ExcInfo]]
    """Creates a hint with the exc info filled in."""
    if exc_info is None:
        exc_info = sys.exc_info()
    else:
        exc_info = exc_info_from_error(exc_info)
    if exc_info[0] is None:
        exc_info = None
    return {"exc_info": exc_info}


class AnnotatedValue:
    """
    Meta information for a data field in the event payload.
    """

    __slots__ = ("value", "metadata")

    def __init__(self, value, metadata):
        # type: (Optional[Any], Dict[str, Any]) -> None
        self.value = value
        self.metadata = metadata

    def __eq__(self, other):
        # type: (Any) -> bool
        if not isinstance(other, AnnotatedValue):
            return False

        return self.value == other.value and self.metadata == other.metadata

    @classmethod
    def removed_because_raw_data(cls):
        # type: () -> AnnotatedValue
        """The value was removed because it could not be parsed. This is done for request body values that are not json nor a form."""
        return AnnotatedValue(
            value="",
            metadata={
                "rem": [  # Remark
                    [
                        "!raw",  # Unparsable raw data
                        "x",  # The fields original value was removed
                    ]
                ]
            },
        )

    @classmethod
    def removed_because_over_size_limit(cls):
        # type: () -> AnnotatedValue
        """The actual value was removed because the size of the field exceeded the configured maximum size (specified with the max_request_body_size sdk option)"""
        return AnnotatedValue(
            value="",
            metadata={
                "rem": [  # Remark
                    [
                        "!config",  # Because of configured maximum size
                        "x",  # The fields original value was removed
                    ]
                ]
            },
        )

    @classmethod
    def substituted_because_contains_sensitive_data(cls):
        # type: () -> AnnotatedValue
        """The actual value was removed because it contained sensitive information."""
        return AnnotatedValue(
            value=SENSITIVE_DATA_SUBSTITUTE,
            metadata={
                "rem": [  # Remark
                    [
                        "!config",  # Because of SDK configuration (in this case the config is the hard coded removal of certain django cookies)
                        "s",  # The fields original value was substituted
                    ]
                ]
            },
        )


if TYPE_CHECKING:
    T = TypeVar("T")
    Annotated = Union[AnnotatedValue, T]


def get_type_name(cls):
    # type: (Optional[type]) -> Optional[str]
    return getattr(cls, "__qualname__", None) or getattr(cls, "__name__", None)


def get_type_module(cls):
    # type: (Optional[type]) -> Optional[str]
    mod = getattr(cls, "__module__", None)
    if mod not in (None, "builtins", "__builtins__"):
        return mod
    return None


def should_hide_frame(frame: "FrameType") -> bool:
    try:
        mod = frame.f_globals["__name__"]
        if mod.startswith("sentry_sdk."):
            return True
    except (AttributeError, KeyError):
        pass

    for flag_name in "__traceback_hide__", "__tracebackhide__":
        try:
            if frame.f_locals[flag_name]:
                return True
        except Exception:
            pass

    return False


def iter_stacks(tb):
    # type: (Optional[TracebackType]) -> Iterator[TracebackType]
    tb_ = tb  # type: Optional[TracebackType]
    while tb_ is not None:
        if not should_hide_frame(tb_.tb_frame):
            yield tb_
        tb_ = tb_.tb_next


def get_lines_from_file(
    filename,  # type: str
    lineno,  # type: int
    max_length=None,  # type: Optional[int]
    loader=None,  # type: Optional[Any]
    module=None,  # type: Optional[str]
):
    # type: (...) -> Tuple[List[Annotated[str]], Optional[Annotated[str]], List[Annotated[str]]]
    context_lines = 5
    source = None
    if loader is not None and hasattr(loader, "get_source"):
        try:
            source_str = loader.get_source(module)  # type: Optional[str]
        except (ImportError, IOError):
            source_str = None
        if source_str is not None:
            source = source_str.splitlines()

    if source is None:
        try:
            source = linecache.getlines(filename)
        except (OSError, IOError):
            return [], None, []

    if not source:
        return [], None, []

    lower_bound = max(0, lineno - context_lines)
    upper_bound = min(lineno + 1 + context_lines, len(source))

    try:
        pre_context = [
            strip_string(line.strip("\r\n"), max_length=max_length)
            for line in source[lower_bound:lineno]
        ]
        context_line = strip_string(source[lineno].strip("\r\n"), max_length=max_length)
        post_context = [
            strip_string(line.strip("\r\n"), max_length=max_length)
            for line in source[(lineno + 1) : upper_bound]  # noqa: E203
        ]
        return pre_context, context_line, post_context
    except IndexError:
        # the file may have changed since it was loaded into memory
        return [], None, []


def get_source_context(
    frame,  # type: FrameType
    tb_lineno,  # type: int
    max_value_length=None,  # type: Optional[int]
):
    # type: (...) -> Tuple[List[Annotated[str]], Optional[Annotated[str]], List[Annotated[str]]]
    try:
        abs_path = frame.f_code.co_filename  # type: Optional[str]
    except Exception:
        abs_path = None
    try:
        module = frame.f_globals["__name__"]
    except Exception:
        return [], None, []
    try:
        loader = frame.f_globals["__loader__"]
    except Exception:
        loader = None
    lineno = tb_lineno - 1
    if lineno is not None and abs_path:
        return get_lines_from_file(
            abs_path, lineno, max_value_length, loader=loader, module=module
        )
    return [], None, []


def safe_str(value):
    # type: (Any) -> str
    try:
        return str(value)
    except Exception:
        return safe_repr(value)


def safe_repr(value):
    # type: (Any) -> str
    try:
        return repr(value)
    except Exception:
        return "<broken repr>"


def filename_for_module(module, abs_path):
    # type: (Optional[str], Optional[str]) -> Optional[str]
    if not abs_path or not module:
        return abs_path

    try:
        if abs_path.endswith(".pyc"):
            abs_path = abs_path[:-1]

        base_module = module.split(".", 1)[0]
        if base_module == module:
            return os.path.basename(abs_path)

        base_module_path = sys.modules[base_module].__file__
        if not base_module_path:
            return abs_path

        return abs_path.split(base_module_path.rsplit(os.sep, 2)[0], 1)[-1].lstrip(
            os.sep
        )
    except Exception:
        return abs_path


def serialize_frame(
    frame,
    tb_lineno=None,
    max_value_length=None,
):
    # type: (FrameType, Optional[int], Optional[int]) -> Dict[str, Any]
    f_code = getattr(frame, "f_code", None)
    if not f_code:
        abs_path = None
        function = None
    else:
        abs_path = frame.f_code.co_filename
        function = frame.f_code.co_name
    try:
        module = frame.f_globals["__name__"]
    except Exception:
        module = None

    if tb_lineno is None:
        tb_lineno = frame.f_lineno

    rv = {
        "platform": "python",
        "filename": filename_for_module(module, abs_path) or None,
        "abs_path": os.path.abspath(abs_path) if abs_path else None,
        "function": function or "<unknown>",
        "module": module,
        "lineno": tb_lineno,
    }  # type: Dict[str, Any]

    rv["pre_context"], rv["context_line"], rv["post_context"] = get_source_context(
        frame, tb_lineno, max_value_length
    )

    return rv


def get_errno(exc_value):
    # type: (BaseException) -> Optional[Any]
    return getattr(exc_value, "errno", None)


def get_error_message(exc_value):
    # type: (Optional[BaseException]) -> str
    message = (
        getattr(exc_value, "message", "")
        or getattr(exc_value, "detail", "")
        or exc_value
    )

    return safe_str(message)


def single_exception_from_error_tuple(
    exc_type,  # type: Optional[type]
    exc_value,  # type: Optional[BaseException]
    tb,  # type: Optional[TracebackType]
    mechanism=None,  # type: Optional[Dict[str, Any]]
    exception_id=None,  # type: Optional[int]
    parent_id=None,  # type: Optional[int]
    source=None,  # type: Optional[str]
):
    # type: (...) -> Dict[str, Any]
    """
    Creates a dict that goes into the events `exception.values` list
    """
    exception_value = {}  # type: Dict[str, Any]
    exception_value["mechanism"] = (
        mechanism.copy() if mechanism else {"type": "generic", "handled": True}
    )
    if exception_id is not None:
        exception_value["mechanism"]["exception_id"] = exception_id

    if exc_value is not None:
        errno = get_errno(exc_value)
    else:
        errno = None

    if errno is not None:
        exception_value["mechanism"].setdefault("meta", {}).setdefault(
            "errno", {}
        ).setdefault("number", errno)

    if source is not None:
        exception_value["mechanism"]["source"] = source

    is_root_exception = exception_id == 0
    if not is_root_exception and parent_id is not None:
        exception_value["mechanism"]["parent_id"] = parent_id
        exception_value["mechanism"]["type"] = "chained"

    if is_root_exception and "type" not in exception_value["mechanism"]:
        exception_value["mechanism"]["type"] = "generic"

    is_exception_group = BaseExceptionGroup is not None and isinstance(
        exc_value, BaseExceptionGroup
    )
    if is_exception_group:
        exception_value["mechanism"]["is_exception_group"] = True

    exception_value["module"] = get_type_module(exc_type)
    exception_value["type"] = get_type_name(exc_type)
    exception_value["value"] = get_error_message(exc_value)

    max_value_length = DEFAULT_MAX_VALUE_LENGTH  # fallback

    frames = [
        serialize_frame(
            tb.tb_frame,
            tb_lineno=tb.tb_lineno,
            max_value_length=max_value_length,
        )
        for tb in iter_stacks(tb)
    ]

    if frames:
        exception_value["stacktrace"] = {"frames": frames, "type": "raw"}

    return exception_value


HAS_CHAINED_EXCEPTIONS = hasattr(Exception, "__suppress_context__")

if HAS_CHAINED_EXCEPTIONS:

    def walk_exception_chain(exc_info):
        # type: (ExcInfo) -> Iterator[ExcInfo]
        exc_type, exc_value, tb = exc_info

        seen_exceptions = []
        seen_exception_ids = set()  # type: Set[int]

        while (
            exc_type is not None
            and exc_value is not None
            and id(exc_value) not in seen_exception_ids
        ):
            yield exc_type, exc_value, tb

            # Avoid hashing random types we don't know anything
            # about. Use the list to keep a ref so that the `id` is
            # not used for another object.
            seen_exceptions.append(exc_value)
            seen_exception_ids.add(id(exc_value))

            if exc_value.__suppress_context__:
                cause = exc_value.__cause__
            else:
                cause = exc_value.__context__
            if cause is None:
                break
            exc_type = type(cause)
            exc_value = cause
            tb = getattr(cause, "__traceback__", None)

else:

    def walk_exception_chain(exc_info):
        # type: (ExcInfo) -> Iterator[ExcInfo]
        yield exc_info


def exceptions_from_error(
    exc_type,  # type: Optional[type]
    exc_value,  # type: Optional[BaseException]
    tb,  # type: Optional[TracebackType]
    mechanism=None,  # type: Optional[Dict[str, Any]]
    exception_id=0,  # type: int
    parent_id=0,  # type: int
    source=None,  # type: Optional[str]
):
    # type: (...) -> Tuple[int, List[Dict[str, Any]]]
    """
    Creates the list of exceptions.
    This can include chained exceptions and exceptions from an ExceptionGroup.
    """

    parent = single_exception_from_error_tuple(
        exc_type=exc_type,
        exc_value=exc_value,
        tb=tb,
        mechanism=mechanism,
        exception_id=exception_id,
        parent_id=parent_id,
        source=source,
    )
    exceptions = [parent]

    parent_id = exception_id
    exception_id += 1

    should_supress_context = (
        hasattr(exc_value, "__suppress_context__") and exc_value.__suppress_context__  # type: ignore
    )
    if should_supress_context:
        # Add direct cause.
        # The field `__cause__` is set when raised with the exception (using the `from` keyword).
        exception_has_cause = (
            exc_value
            and hasattr(exc_value, "__cause__")
            and exc_value.__cause__ is not None
        )
        if exception_has_cause:
            cause = exc_value.__cause__  # type: ignore
            (exception_id, child_exceptions) = exceptions_from_error(
                exc_type=type(cause),
                exc_value=cause,
                tb=getattr(cause, "__traceback__", None),
                mechanism=mechanism,
                exception_id=exception_id,
                source="__cause__",
            )
            exceptions.extend(child_exceptions)

    else:
        # Add indirect cause.
        # The field `__context__` is assigned if another exception occurs while handling the exception.
        exception_has_content = (
            exc_value
            and hasattr(exc_value, "__context__")
            and exc_value.__context__ is not None
        )
        if exception_has_content:
            context = exc_value.__context__  # type: ignore
            (exception_id, child_exceptions) = exceptions_from_error(
                exc_type=type(context),
                exc_value=context,
                tb=getattr(context, "__traceback__", None),
                mechanism=mechanism,
                exception_id=exception_id,
                source="__context__",
            )
            exceptions.extend(child_exceptions)

    # Add exceptions from an ExceptionGroup.
    is_exception_group = exc_value and hasattr(exc_value, "exceptions")
    if is_exception_group:
        for idx, e in enumerate(exc_value.exceptions):  # type: ignore
            (exception_id, child_exceptions) = exceptions_from_error(
                exc_type=type(e),
                exc_value=e,
                tb=getattr(e, "__traceback__", None),
                mechanism=mechanism,
                exception_id=exception_id,
                parent_id=parent_id,
                source="exceptions[%s]" % idx,
            )
            exceptions.extend(child_exceptions)

    return (exception_id, exceptions)


def exceptions_from_error_tuple(
    exc_info,  # type: ExcInfo
    mechanism=None,  # type: Optional[Dict[str, Any]]
):
    # type: (...) -> List[Dict[str, Any]]
    exc_type, exc_value, tb = exc_info

    is_exception_group = BaseExceptionGroup is not None and isinstance(
        exc_value, BaseExceptionGroup
    )

    if is_exception_group:
        (_, exceptions) = exceptions_from_error(
            exc_type=exc_type,
            exc_value=exc_value,
            tb=tb,
            mechanism=mechanism,
            exception_id=0,
            parent_id=0,
        )

    else:
        exceptions = []
        for exc_type, exc_value, tb in walk_exception_chain(exc_info):
            exceptions.append(
                single_exception_from_error_tuple(exc_type, exc_value, tb, mechanism)
            )

    exceptions.reverse()

    return exceptions


def to_string(value):
    # type: (str) -> str
    try:
        return str(value)
    except UnicodeDecodeError:
        return repr(value)[1:-1]


def iter_event_stacktraces(event):
    # type: (Event) -> Iterator[Dict[str, Any]]
    if "stacktrace" in event:
        yield event["stacktrace"]
    if "threads" in event:
        for thread in event["threads"].get("values") or ():
            if "stacktrace" in thread:
                yield thread["stacktrace"]
    if "exception" in event:
        for exception in event["exception"].get("values") or ():
            if "stacktrace" in exception:
                yield exception["stacktrace"]


def iter_event_frames(event):
    # type: (Event) -> Iterator[Dict[str, Any]]
    for stacktrace in iter_event_stacktraces(event):
        for frame in stacktrace.get("frames") or ():
            yield frame


def handle_in_app(event, in_app_exclude=None, in_app_include=None, project_root=None):
    # type: (Event, Optional[List[str]], Optional[List[str]], Optional[str]) -> Event
    for stacktrace in iter_event_stacktraces(event):
        set_in_app_in_frames(
            stacktrace.get("frames"),
            in_app_exclude=in_app_exclude,
            in_app_include=in_app_include,
            project_root=project_root,
        )

    return event


def set_in_app_in_frames(frames, in_app_exclude, in_app_include, project_root=None):
    # type: (Any, Optional[List[str]], Optional[List[str]], Optional[str]) -> Optional[Any]
    if not frames:
        return None

    for frame in frames:
        # if frame has already been marked as in_app, skip it
        current_in_app = frame.get("in_app")
        if current_in_app is not None:
            continue

        module = frame.get("module")

        # check if module in frame is in the list of modules to include
        if _module_in_list(module, in_app_include):
            frame["in_app"] = True
            continue

        # check if module in frame is in the list of modules to exclude
        if _module_in_list(module, in_app_exclude):
            frame["in_app"] = False
            continue

        # if frame has no abs_path, skip further checks
        abs_path = frame.get("abs_path")
        if abs_path is None:
            continue

        if _is_external_source(abs_path):
            frame["in_app"] = False
            continue

        if _is_in_project_root(abs_path, project_root):
            frame["in_app"] = True
            continue

    return frames


def exception_is_already_captured(error):
    # type: (ExceptionArg) -> bool
    if isinstance(error, BaseException):
        return hasattr(error, "__posthog_exception_captured")
    # Autocaptured exceptions are passed as a tuple from our system hooks,
    # the second item is the exception value (the first is the exception type)
    elif isinstance(error, tuple) and len(error) > 1:
        return error[1] is not None and hasattr(
            error[1], "__posthog_exception_captured"
        )
    else:
        return False  # type: ignore[unreachable]


def mark_exception_as_captured(error, uuid):
    # type: (ExceptionArg, str) -> None
    if isinstance(error, BaseException):
        setattr(error, "__posthog_exception_captured", True)
        setattr(error, "__posthog_exception_uuid", uuid)
    # Autocaptured exceptions are passed as a tuple from our system hooks,
    # the second item is the exception value (the first is the exception type)
    elif isinstance(error, tuple) and len(error) > 1:
        if error[1] is not None:
            setattr(error[1], "__posthog_exception_captured", True)
            setattr(error[1], "__posthog_exception_uuid", uuid)


def exc_info_from_error(error):
    # type: (ExceptionArg) -> ExcInfo
    if isinstance(error, tuple) and len(error) == 3:
        exc_type, exc_value, tb = error
    elif isinstance(error, BaseException):
        try:
            construct_artificial_traceback(error)
        except Exception:
            pass
        tb = getattr(error, "__traceback__", None)
        if tb is not None:
            exc_type = type(error)
            exc_value = error
        else:
            exc_type, exc_value, tb = sys.exc_info()
            if exc_value is not error:
                tb = None
                exc_value = error
                exc_type = type(error)

    else:
        raise ValueError("Expected Exception object to report, got %s!" % type(error))

    exc_info = (exc_type, exc_value, tb)

    if TYPE_CHECKING:
        # This cast is safe because exc_type and exc_value are either both
        # None or both not None.
        exc_info = cast(ExcInfo, exc_info)

    return exc_info


def construct_artificial_traceback(e):
    # type: (BaseException) -> None
    if getattr(e, "__traceback__", None) is not None:
        return

    depth = 0
    frames = []
    while True:
        try:
            frame = sys._getframe(depth)
            depth += 1
        except ValueError:
            break

        frames.append(frame)

    frames.reverse()

    tb = None
    for frame in frames:
        tb = types.TracebackType(tb, frame, frame.f_lasti, frame.f_lineno)

    setattr(e, "__traceback__", tb)


def _module_in_list(name, items):
    # type: (str | None, Optional[List[str]]) -> bool
    if name is None:
        return False

    if not items:
        return False

    for item in items:
        if item == name or name.startswith(item + "."):
            return True

    return False


def _is_external_source(abs_path):
    # type: (str) -> bool
    # check if frame is in 'site-packages' or 'dist-packages'
    external_source = (
        re.search(r"[\\/](?:dist|site)-packages[\\/]", abs_path) is not None
    )
    return external_source


def _is_in_project_root(abs_path, project_root):
    # type: (str, Optional[str]) -> bool
    if project_root is None:
        return False

    # check if path is in the project root
    if abs_path.startswith(project_root):
        return True

    return False


def _truncate_by_bytes(string, max_bytes):
    # type: (str, int) -> str
    """
    Truncate a UTF-8-encodable string to the last full codepoint so that it fits in max_bytes.
    """
    truncated = string.encode("utf-8")[: max_bytes - 3].decode("utf-8", errors="ignore")

    return truncated + "..."


def _get_size_in_bytes(value):
    # type: (str) -> Optional[int]
    try:
        return len(value.encode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def strip_string(value, max_length=None):
    # type: (str, Optional[int]) -> Union[AnnotatedValue, str]
    if not value:
        return value

    if max_length is None:
        max_length = DEFAULT_MAX_VALUE_LENGTH

    byte_size = _get_size_in_bytes(value)
    text_size = len(value)

    if byte_size is not None and byte_size > max_length:
        # truncate to max_length bytes, preserving code points
        truncated_value = _truncate_by_bytes(value, max_length)
    elif text_size is not None and text_size > max_length:
        # fallback to truncating by string length
        truncated_value = value[: max_length - 3] + "..."
    else:
        return value

    return AnnotatedValue(
        value=truncated_value,
        metadata={
            "len": byte_size or text_size,
            "rem": [["!limit", "x", max_length - 3, max_length]],
        },
    )


def _extract_plain_substring(pattern):
    # Matches inline flag groups like (?i), (?ai), (?ims), etc. that include the 'i' flag.
    # Python regex flags: a=ASCII, i=IGNORECASE, L=LOCALE, m=MULTILINE, s=DOTALL, u=UNICODE, x=VERBOSE
    inline_flags = re.match(r"^\(\?[aiLmsux]*i[aiLmsux]*\)", pattern)
    if not inline_flags:
        return None
    remainder = pattern[inline_flags.end() :]
    if not remainder or any(c in _REGEX_METACHARACTERS for c in remainder):
        return None
    return remainder.lower()


def _compile_patterns_impl(patterns):
    if not patterns:
        return None
    substrings = []
    regexes = []
    for pattern in patterns:
        simple = _extract_plain_substring(pattern)
        if simple is not None:
            substrings.append(simple)
        else:
            try:
                regexes.append(re.compile(pattern))
            except Exception:
                pass
    if not substrings and not regexes:
        return None
    return (substrings, regexes)


@functools.lru_cache(maxsize=256)
def _compile_patterns_cached(patterns_tuple):
    return _compile_patterns_impl(patterns_tuple)


def _compile_patterns(patterns):
    # Cache by content so the default pattern set compiles once; fall back to an uncached
    # compile for exotic, unhashable custom input.
    try:
        return _compile_patterns_cached(tuple(patterns))
    except TypeError:
        return _compile_patterns_impl(list(patterns))


def _pattern_matches(name, patterns):
    if patterns is None:
        return False
    substrings, regexes = patterns
    if substrings:
        name_lower = name.lower()
        for s in substrings:
            if s in name_lower:
                return True
    for pattern in regexes:
        if pattern.search(name):
            return True
    return False


def _build_matcher(compiled):
    """Collapse a compiled ``(substrings, regexes)`` pair into a fast single-call matcher
    by compiling the literal substrings into one alternation regex. Returns ``None`` when
    there is nothing to match."""
    if compiled is None:
        return None
    substrings, regexes = compiled
    return (_compile_substring_alternation(tuple(substrings)), regexes)


@functools.lru_cache(maxsize=256)
def _compile_substring_alternation(substrings):
    # Substrings are already lowercased and matched against the lowercased name, so a
    # case-sensitive alternation suffices and avoids IGNORECASE's per-character case-folding.
    if not substrings:
        return None
    return re.compile("|".join(re.escape(s) for s in substrings))


def _matcher_matches(name, matcher):
    """Hot-path equivalent of ``_pattern_matches`` for a ``_build_matcher`` matcher."""
    if matcher is None:
        return False
    substr_re, regexes = matcher
    if substr_re is not None and substr_re.search(name.lower()):
        return True
    for pattern in regexes:
        if pattern.search(name):
            return True
    return False


@dataclasses.dataclass(frozen=True)
class _MaskingConfig:
    """Everything the masking pipeline needs, compiled once per capture and threaded
    through instead of recompiling patterns per frame."""

    mask: Optional[
        Tuple[Optional[Pattern], List[Pattern]]
    ]  # name/value redaction matcher
    ignore: Optional[
        Tuple[Optional[Pattern], List[Pattern]]
    ]  # variable-name skip matcher
    mask_url_credentials: bool
    max_length: int = DEFAULT_MAX_VALUE_LENGTH

    @classmethod
    def build(
        cls,
        mask_patterns=None,
        ignore_patterns=None,
        mask_url_credentials=True,
        max_length=DEFAULT_MAX_VALUE_LENGTH,
    ):
        # type: (...) -> _MaskingConfig
        return cls(
            mask=_build_matcher(_compile_patterns(mask_patterns or [])),
            ignore=_build_matcher(_compile_patterns(ignore_patterns or [])),
            mask_url_credentials=mask_url_credentials,
            max_length=max_length,
        )


def _mask_string(value, config):
    """Apply the string masking policy: over-length cap, name/value patterns, then
    embedded URL credentials."""
    if len(value) > _MAX_VALUE_LENGTH_FOR_PATTERN_MATCH:
        return CODE_VARIABLES_TOO_LONG_VALUE
    if _matcher_matches(value, config.mask):
        return CODE_VARIABLES_REDACTED_VALUE
    if config.mask_url_credentials:
        return _redact_url_credentials(value)
    return value


def _safe_type_name(value):
    try:
        return type(value).__qualname__
    except Exception:
        return "unknown"


def _safe_repr(value, config):
    """Last-resort serialization for values we can't structurally decompose. Renders
    ``repr(value)`` but fails closed: redact entirely on any mask match, over-length
    repr, or a raising ``__repr__``."""
    try:
        rendered = repr(value)
    except Exception:
        return "<" + _safe_type_name(value) + ">"

    # Too long to scan, so we can't vouch for it -> redact it all.
    if len(rendered) > _MAX_VALUE_LENGTH_FOR_PATTERN_MATCH:
        return CODE_VARIABLES_REDACTED_VALUE
    if _matcher_matches(rendered, config.mask):
        return CODE_VARIABLES_REDACTED_VALUE
    if config.mask_url_credentials:
        return _redact_url_credentials(rendered)
    return rendered


def _extract_object_attrs(value):
    """Return a ``name -> value`` mapping of an object's attributes, or ``None`` for
    values that should be treated as opaque leaves (built-ins, slotted objects, empty
    ``__dict__``)."""
    if isinstance(value, type):
        # A class/type object itself, not an instance - nothing useful to traverse.
        return None
    try:
        if dataclasses.is_dataclass(value):
            return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
        instance_dict = getattr(value, "__dict__", None)
    except Exception:
        return None
    if isinstance(instance_dict, dict) and instance_dict:
        # Copy so we never mutate the live object; keys here are attribute names.
        return dict(instance_dict)
    return None


# Method/function descriptor types excluded when scanning a class for sensitively-named
# members, so an object isn't redacted merely for having e.g. an `authenticate()` method.
_METHOD_MEMBER_TYPES = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.MethodDescriptorType,
    types.WrapperDescriptorType,
    types.MethodWrapperType,
    types.ClassMethodDescriptorType,
    classmethod,
    staticmethod,
)


def _is_data_member(attr):
    """True for a class member that holds or produces a value (class attribute,
    ``@property``, descriptor) as opposed to a method or nested class."""
    return not isinstance(attr, type) and not isinstance(attr, _METHOD_MEMBER_TYPES)


def _masked_type_members(value, config):
    """Redact sensitively-named members declared on the object's type (class attributes,
    ``@property``, ``__slots__`` entries) which live on the class, not instance
    ``__dict__``. Only member *names* are read, never the getters."""
    if config.mask is None:
        return {}
    try:
        mro = type(value).__mro__
    except Exception:
        return {}
    masked = {}
    for klass in mro:
        for name, attr in klass.__dict__.items():
            if (
                isinstance(name, str)
                and name not in masked
                and _is_data_member(attr)
                and _matcher_matches(name, config.mask)
            ):
                masked[name] = CODE_VARIABLES_REDACTED_VALUE
    return masked


def _mask_mapping(items, config, seen, depth):
    """Mask a sequence of ``(key, value)`` pairs into a dict. A key matching the mask
    redacts its value; surviving values recurse through ``_mask_value``. Keys are kept
    JSON-serializable."""
    result = {}
    for key, value in items:
        if type(key) is str:
            out_key = key_str = key
        else:
            key_str = key if isinstance(key, str) else str(key)
            # json.dumps only accepts str/int/float/bool/None keys; coerce anything else to
            # its string form so one exotic key can't make json.dumps fail.
            key_is_json_safe = (
                key is None
                or isinstance(key, (str, int))  # bool is an int subclass
                or (isinstance(key, float) and math.isfinite(key))
            )
            out_key = key if key_is_json_safe else key_str
        if len(key_str) > _MAX_VALUE_LENGTH_FOR_PATTERN_MATCH:
            result[out_key] = CODE_VARIABLES_TOO_LONG_VALUE
        elif _matcher_matches(key_str, config.mask):
            result[out_key] = CODE_VARIABLES_REDACTED_VALUE
        else:
            result[out_key] = _mask_value(value, config, seen, depth + 1)
    return result


def _mask_value(value, config, seen=None, depth=0):
    """Turn any Python value into a JSON-safe, masked value. Single source of truth for
    what gets redacted; the result contains only JSON-native types so it can be handed
    straight to ``json.dumps``."""
    # Name masking and URL scrubbing are independent toggles; skip only when both are off.
    if config.mask is None and not config.mask_url_credentials:
        return value

    # Exact-type fast paths for plain scalars, avoiding the isinstance ladder below.
    # Subclasses fall through to the isinstance checks, so output is unchanged.
    t = type(value)
    if t is str:
        return _mask_string(value, config)
    if t is int or t is bool:
        return value
    if t is float:
        # Non-finite floats (NaN/Infinity) are invalid JSON, so render them as strings.
        return value if math.isfinite(value) else str(value)
    if value is None:
        return value

    if t is not dict and t is not list and t is not tuple:
        # A scalar subclass (e.g. IntEnum, str subclass) - handle before traversing.
        if isinstance(value, str):
            return _mask_string(value, config)
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, (bool, int, float)):
            return value

    if depth >= _MAX_MASK_DEPTH:
        # Too deep to keep traversing; fail closed rather than repr (which could leak).
        return CODE_VARIABLES_TOO_LONG_VALUE

    if seen is None:
        seen = set()
    obj_id = id(value)
    if obj_id in seen:
        return "<circular ref>"
    seen.add(obj_id)

    if len(seen) > _MAX_TOTAL_NODES_TO_MASK:
        return CODE_VARIABLES_TOO_LONG_VALUE

    if t is dict or isinstance(value, dict):
        if len(value) > _MAX_COLLECTION_ITEMS_TO_SCAN:
            return CODE_VARIABLES_TOO_LONG_VALUE
        return _mask_mapping(value.items(), config, seen, depth)

    # namedtuples are tuples but their fields have names: traverse like an object (so a
    # field named `password` is caught) and emit a dict the encoder can serialize directly.
    if isinstance(value, tuple) and hasattr(value, "_fields"):
        fields = value._fields
        if len(fields) > _MAX_COLLECTION_ITEMS_TO_SCAN:
            return CODE_VARIABLES_TOO_LONG_VALUE
        masked = _mask_mapping(zip(fields, value), config, seen, depth)
        masked["__class__"] = _safe_type_name(value)
        return masked

    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_COLLECTION_ITEMS_TO_SCAN:
            return CODE_VARIABLES_TOO_LONG_VALUE
        masked_items = [_mask_value(item, config, seen, depth + 1) for item in value]
        try:
            return type(value)(masked_items)
        except Exception:
            # A list/tuple subclass whose constructor rejects a single iterable; the items
            # are already masked, so fall back to a plain list.
            return masked_items

    # Custom objects: traverse their real attributes so a field named e.g. `password` is
    # caught by name, rather than repr-scanning (which a custom __repr__ could relabel).
    attrs = _extract_object_attrs(value)
    if attrs is not None:
        if len(attrs) > _MAX_COLLECTION_ITEMS_TO_SCAN:
            return CODE_VARIABLES_TOO_LONG_VALUE
        masked = _mask_mapping(attrs.items(), config, seen, depth)
        masked["__class__"] = _safe_type_name(value)
        return masked

    # A custom __repr__ can expose secrets held on the *class* (class attribute, @property,
    # __slots__ entry) that attribute traversal never sees; redact those by name first.
    masked_members = _masked_type_members(value, config)
    if masked_members:
        masked_members["__class__"] = _safe_type_name(value)
        return masked_members

    # Opaque leaf (built-in/slotted/etc.): fall back to a fail-closed repr.
    return _safe_repr(value, config)


def _finalize(result, limiter, max_length):
    """Truncate to ``max_length`` and charge against the size budget; ``None`` when spent."""
    if len(result) > max_length:
        result = result[: max_length - 3] + "..."
    if not limiter.can_add(len(result)):
        return None
    limiter.add(len(result))
    return result


def _encode_variable(value, config, limiter):
    """Format one already-masked variable for the wire: finite numbers stay raw JSON
    numbers, everything else becomes a string. ``None`` when the size budget is spent."""
    try:
        safe = _mask_value(value, config)

        if safe is None:
            result = "None"
        elif isinstance(safe, bool):
            result = str(safe)
        elif isinstance(safe, float) and not math.isfinite(safe):
            # Only reachable when masking is disabled; keep NaN/Infinity out of the JSON.
            result = str(safe)
        elif isinstance(safe, (int, float)):
            # Numbers are emitted as raw JSON numbers, so they skip string truncation.
            result_size = len(str(safe))
            if not limiter.can_add(result_size):
                return None
            limiter.add(result_size)
            return safe
        elif isinstance(safe, str):
            result = safe
        else:
            # `default` is a safety net for anything _mask_value left non-serializable.
            result = json.dumps(
                safe,
                default=lambda o: _safe_repr(o, config),
                allow_nan=False,
            )

        return _finalize(result, limiter, config.max_length)
    except Exception:
        # Fail closed: even if json.dumps chokes, re-render through the masking-aware repr.
        try:
            rendered = _safe_repr(value, config)
        except Exception:
            rendered = f"<{_safe_type_name(value)}>"
        return _finalize(rendered, limiter, config.max_length)


def _is_simple_type(value):
    return isinstance(value, (type(None), bool, int, float, str))


def _add_variable(result, name, value, config, limiter):
    """Add one masked variable to ``result``; returns False when the budget is spent. A
    variable whose *name* matches the mask is redacted whole, value unread."""
    if _matcher_matches(name, config.mask):
        if not limiter.can_add(len(CODE_VARIABLES_REDACTED_VALUE)):
            return False
        limiter.add(len(CODE_VARIABLES_REDACTED_VALUE))
        result[name] = CODE_VARIABLES_REDACTED_VALUE
        return True

    encoded = _encode_variable(value, config, limiter)
    if encoded is None:
        return False
    result[name] = encoded
    return True


def _serialize_frame_variables(frame, limiter, config):
    """Serialize one frame's locals into a ``name -> wire value`` dict. Scalars are
    emitted before complex values so the cheapest context survives a tight size budget."""
    try:
        local_vars = frame.f_locals.copy()
    except Exception:
        return {}

    simple: Dict[str, Any] = {}
    complex_: Dict[str, Any] = {}
    for name, value in local_vars.items():
        if _matcher_matches(name, config.ignore):
            continue
        (simple if _is_simple_type(value) else complex_)[name] = value

    result: Dict[str, Any] = {}
    for name in sorted(simple):
        if not _add_variable(result, name, simple[name], config, limiter):
            return result
    for name in sorted(complex_):
        if not _add_variable(result, name, complex_[name], config, limiter):
            return result
    return result


def serialize_code_variables(
    frame,
    limiter,
    mask_patterns=None,
    ignore_patterns=None,
    max_length=1024,
    mask_url_credentials=True,
):
    """Serialize a single frame's locals. Convenience wrapper that builds a one-off config;
    the hot path builds the config once - see ``attach_code_variables_to_frames``."""
    config = _MaskingConfig.build(
        mask_patterns=mask_patterns,
        ignore_patterns=ignore_patterns,
        mask_url_credentials=mask_url_credentials,
        max_length=max_length,
    )
    return _serialize_frame_variables(frame, limiter, config)


def try_attach_code_variables_to_frames(
    all_exceptions, exc_info, mask_patterns, ignore_patterns, mask_url_credentials=True
):
    try:
        attach_code_variables_to_frames(
            all_exceptions,
            exc_info,
            mask_patterns,
            ignore_patterns,
            mask_url_credentials,
        )
    except Exception:
        pass


def attach_code_variables_to_frames(
    all_exceptions, exc_info, mask_patterns, ignore_patterns, mask_url_credentials=True
):
    exc_type, exc_value, traceback = exc_info

    if traceback is None:
        return

    tb_frames = list(iter_stacks(traceback))

    if not tb_frames:
        return

    # Compile patterns once for the whole capture and share one budget across all frames.
    config = _MaskingConfig.build(
        mask_patterns=mask_patterns,
        ignore_patterns=ignore_patterns,
        mask_url_credentials=mask_url_credentials,
    )
    limiter = VariableSizeLimiter()

    for exception in all_exceptions:
        stacktrace = exception.get("stacktrace")
        if not stacktrace or "frames" not in stacktrace:
            continue

        for serialized_frame, tb_item in zip(stacktrace["frames"], tb_frames):
            if not serialized_frame.get("in_app"):
                continue

            variables = _serialize_frame_variables(tb_item.tb_frame, limiter, config)
            if variables:
                serialized_frame["code_variables"] = variables
