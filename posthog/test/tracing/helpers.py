"""Shared fakes for the tracing pipeline and export tests."""

import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace
from unittest import mock

import pytest

from posthog.tracing import _export as export_module
from posthog.tracing._config import resolve_traces_config
from posthog.tracing._drops import DropLog
from posthog.tracing._export import SpanExporter
from posthog.tracing._pipeline import PostHogTraces
from posthog.tracing._transport import SendOutcome

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"

RealTimer = threading.Timer


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


class FakeSender:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.payloads: list = []

    def __call__(self, client, payload):
        self.payloads.append(payload)
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0] if self.outcomes else SendOutcome("ok")

    def batches(self):
        return [p["resourceSpans"][0]["scopeSpans"][0]["spans"] for p in self.payloads]


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


@pytest.fixture(autouse=True)
def no_jitter():
    # Backoff delays are asserted exactly; TestJitter covers the spread.
    with mock.patch.object(export_module, "_draw_jitter", return_value=1.0):
        yield


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


def make_traces(sender=None, client=None, context=None, **config):
    """A pipeline over a real exporter whose sender is ``sender``."""
    config.setdefault("flush_interval", 5)
    client = client or SimpleNamespace(disabled=False, send=True)
    sender = sender or FakeSender(SendOutcome("ok"))
    active: ContextVar = ContextVar("active", default=None)
    resolved = resolve_traces_config(config)
    drops = DropLog(resolved.flush_interval)
    pipeline = PostHogTraces(
        client,
        resolved,
        lambda: context or {},
        active,
        SpanExporter(client, resolved, drops, send=sender),
        drops,
    )
    return pipeline, sender, active


def queued(pipeline):
    exporter = pipeline._exporter
    if isinstance(exporter, RecordingExporter):
        return exporter.records
    return exporter._queue
