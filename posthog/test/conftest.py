from __future__ import annotations

import pytest

import posthog.client as client_module


@pytest.fixture(autouse=True)
def disable_client_atexit_join(monkeypatch):
    monkeypatch.setattr(client_module.atexit, "register", lambda *args, **kwargs: None)
