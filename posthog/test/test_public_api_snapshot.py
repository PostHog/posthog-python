from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHECK_PUBLIC_API_PATH = ROOT / ".github" / "scripts" / "check_public_api.py"


def load_check_public_api():
    spec = importlib.util.spec_from_file_location(
        "check_public_api", CHECK_PUBLIC_API_PATH
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_public_api = load_check_public_api()


@pytest.mark.parametrize(
    ("path", "placeholder"),
    check_public_api.ATTRIBUTE_VALUE_PLACEHOLDERS.items(),
)
def test_attribute_details_uses_placeholder_values(path, placeholder):
    obj = SimpleNamespace(path=path, annotation=None, value='"7.19.1"')

    assert check_public_api._attribute_details(obj) == f"{path} = {placeholder}"
