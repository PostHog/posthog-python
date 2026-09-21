from __future__ import annotations

import pytest

import posthog.client as client_module


@pytest.fixture(autouse=True)
def disable_remote_config_for_unrelated_tests(monkeypatch, request):
    if request.module.__name__ != "posthog.test.test_project_remote_config":
        monkeypatch.setattr(
            client_module.Client, "_start_remote_config", lambda self: None
        )


@pytest.fixture(autouse=True)
def disable_client_atexit_join(monkeypatch):
    monkeypatch.setattr(client_module.atexit, "register", lambda *args, **kwargs: None)
