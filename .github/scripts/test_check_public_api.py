#!/usr/bin/env python3
"""Tests for check_public_api.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).with_name("check_public_api.py")


def load_check_public_api():
    spec = importlib.util.spec_from_file_location("check_public_api", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_attribute_details_uses_placeholder_values() -> None:
    check_public_api = load_check_public_api()

    for path, placeholder in check_public_api.ATTRIBUTE_VALUE_PLACEHOLDERS.items():
        obj = SimpleNamespace(path=path, annotation=None, value='"7.19.1"')
        assert check_public_api._attribute_details(obj) == f"{path} = {placeholder}"


def main() -> int:
    test_attribute_details_uses_placeholder_values()
    print("check_public_api tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
