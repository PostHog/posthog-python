"""Shared fakes for the tracing pipeline tests."""

import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace
from unittest import mock

import pytest

from posthog.tracing._config import resolve_traces_config
from posthog.tracing._drops import DropLog
from posthog.tracing._pipeline import PostHogTraces

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


class FakeTimer:
    """Records the delay it was armed with; fires only when a test says so."""

    instances: list = []

    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn()


class RecordingExporter:
    """Stands in for the export queue: keeps every record it is handed."""

    def __init__(self):
        self.records: list = []
        self.closed = False
        self.reinitialized = False

    def enqueue(self, record):
        self.records.append(record)

    def flush(self, timeout=None):
        pass

    def close(self):
        self.closed = True

    def warn_if_queued(self):
        pass

    def reinit_after_fork(self):
        self.reinitialized = True


@pytest.fixture(autouse=True)
def fake_timers():
    FakeTimer.instances = []
    with mock.patch.object(threading, "Timer", FakeTimer):
        yield FakeTimer


@pytest.fixture
def clock():
    state = {"now": 1000.0}
    with mock.patch.object(time, "monotonic", lambda: state["now"]):
        yield state


def make(client=None, context=None, **config):
    """A pipeline whose ended spans collect on a ``RecordingExporter``."""
    client = client or SimpleNamespace(disabled=False, send=True)
    exporter = RecordingExporter()
    active: ContextVar = ContextVar("active", default=None)
    resolved = resolve_traces_config(config)
    drops = DropLog(resolved.flush_interval)
    pipeline = PostHogTraces(
        client, resolved, lambda: context or {}, active, exporter, drops
    )
    return pipeline, exporter, active


def queued(pipeline):
    return pipeline._exporter.records
