"""Fixtures shared by the tracing tests."""

import threading
import time
from unittest import mock

import pytest

from posthog.test.tracing.helpers import FakeTimer
from posthog.tracing import _export as export_module


@pytest.fixture
def fake_timers():
    """Timers that fire only when a test says so."""
    FakeTimer.instances = []
    with mock.patch.object(threading, "Timer", FakeTimer):
        yield FakeTimer


@pytest.fixture
def no_jitter():
    # Backoff delays are asserted exactly; TestJitter covers the spread.
    with mock.patch.object(export_module, "_draw_jitter", return_value=1.0):
        yield


@pytest.fixture
def clock():
    state = {"now": 1000.0}
    with mock.patch.object(time, "monotonic", lambda: state["now"]):
        yield state
