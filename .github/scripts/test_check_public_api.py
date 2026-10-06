#!/usr/bin/env python3
"""Tests for check_public_api.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory


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

    obj = SimpleNamespace(
        path="posthog.version.VERSION", annotation=None, value='"7.19.1"'
    )
    assert (
        check_public_api._attribute_details(obj)
        == "posthog.version.VERSION = <version>"
    )

    for path, placeholder in check_public_api.ATTRIBUTE_VALUE_PLACEHOLDERS.items():
        entry = SimpleNamespace(path=path, annotation=None, value='"7.19.1"')
        assert check_public_api._attribute_details(entry) == f"{path} = {placeholder}"

    obj.path = "posthog.other.CONSTANT"
    assert (
        check_public_api._attribute_details(obj) == 'posthog.other.CONSTANT = "7.19.1"'
    )


def test_explicit_exports_from_private_modules_include_signatures() -> None:
    import griffe

    check_public_api = load_check_public_api()
    with TemporaryDirectory() as directory:
        package = Path(directory) / "posthog"
        package.mkdir()
        (package / "__init__.py").write_text(
            'from ._implementation import Client, Hidden\n__all__ = ["Client"]\n'
        )
        (package / "_implementation.py").write_text(
            "class Client:\n"
            "    def __init__(self, *, key: str): pass\n"
            "    def upload(self, value: float) -> bool: return True\n"
            "    def _internal(self): pass\n"
            "class Hidden: pass\n"
        )
        module = griffe.load(
            "posthog",
            search_paths=[directory],
            allow_inspection=False,
            try_relative_path=False,
        )
        records = [
            check_public_api._record(obj)
            for obj in check_public_api._iter_module_members(module)
        ]
    assert "class posthog.Client(*, key: str)" in records
    assert "method posthog.Client.upload(value: float)" in records
    assert not any("Hidden" in record or "_internal" in record for record in records)


def main() -> int:
    test_attribute_details_uses_placeholder_values()
    test_explicit_exports_from_private_modules_include_signatures()
    print("check_public_api tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
