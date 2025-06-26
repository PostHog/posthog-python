from typing import TypedDict, Optional, Any, Dict, Union, Tuple, Type
from types import TracebackType
from typing_extensions import NotRequired  # For Python < 3.11 compatibility
from datetime import datetime
import numbers
from uuid import UUID

ID_TYPES = Union[numbers.Number, str, UUID, int]


class OptionalCaptureArgs(TypedDict):
    """Optional arguments for the capture method."""

    distinct_id: NotRequired[Optional[ID_TYPES]]
    properties: NotRequired[Optional[Dict[str, Any]]]
    timestamp: NotRequired[Optional[Union[datetime, str]]]
    uuid: NotRequired[Optional[str]]
    groups: NotRequired[Optional[Dict[str, str]]]
    send_feature_flags: NotRequired[bool]
    disable_geoip: NotRequired[Optional[bool]]


class OptionalSetArgs(TypedDict):
    """Optional arguments for the set method."""

    distinct_id: NotRequired[Optional[ID_TYPES]]
    properties: NotRequired[Optional[Dict[str, Any]]]
    timestamp: NotRequired[Optional[Union[datetime, str]]]
    uuid: NotRequired[Optional[str]]
    disable_geoip: NotRequired[Optional[bool]]


ExcInfo = Union[
    Tuple[Type[BaseException], BaseException, Optional[TracebackType]],
    Tuple[None, None, None],
]

ExceptionArg = Union[BaseException, ExcInfo]
