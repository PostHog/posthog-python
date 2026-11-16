#!/usr/bin/env python3
"""Tests for check_public_api.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from textwrap import dedent

import pytest


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


def _snapshot_for_source(tmp_path, source):
    package = tmp_path / "posthog"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(dedent(source))
    checker = load_check_public_api()
    checker.ROOT = tmp_path
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        return checker.generate_snapshot()


def test_constructor_helpers_preserve_public_attributes(tmp_path):
    snapshot = _snapshot_for_source(
        tmp_path,
        """
        class Client:
            def __init__(self, key, before_send):
                self._configure(key)
                self._set_before_send(before_send)
            def _configure(self, key):
                self.secret_key: str = key
                self._configure_more()
            def _configure_more(self):
                self.options = dict(enabled=True)
            def _set_before_send(self, before_send):
                self.before_send = before_send
        """,
    )
    assert "attribute posthog.Client.secret_key: str = key\n" in snapshot
    assert "attribute posthog.Client.options = dict(enabled=True)\n" in snapshot
    assert "attribute posthog.Client.before_send = before_send\n" in snapshot
    assert "_configure" not in snapshot


def test_constructor_helper_cycles_do_not_leak_class_context(tmp_path):
    snapshot = _snapshot_for_source(
        tmp_path,
        """
        class First:
            def __init__(self): self._one()
            def _one(self):
                self.one = 1
                self._two()
            def _two(self):
                self.two = 2
                self._one()
        class Second:
            def __init__(self): self.three = 3
        module_value = 4
        """,
    )
    assert "attribute posthog.First.one = 1\n" in snapshot
    assert "attribute posthog.First.two = 2\n" in snapshot
    assert "attribute posthog.Second.three = 3\n" in snapshot
    assert "attribute posthog.module_value = 4\n" in snapshot
    assert "posthog.First.three" not in snapshot


def test_uncalled_and_deferred_helpers_do_not_add_attributes(tmp_path):
    snapshot = _snapshot_for_source(
        tmp_path,
        """
        class Client:
            def __init__(self):
                self._configure()
                saved = self._unrelated
                later = lambda: self._lambda_only()
                deferred = (self._generator_only() for _ in range(1))
                def nested(): self._nested_only()
                class Inner:
                    def run(inner): self._inner_only()
                self.public()
                other._foreign()
                self._static()
                self._async()
            def _configure(self):
                self.visible = True
                self._private = False
            def _unrelated(self): self.unrelated = True
            def _lambda_only(self): self.lambda_only = True
            def _generator_only(self): self.generator_only = True
            def _nested_only(self): self.nested_only = True
            def _inner_only(self): self.inner_only = True
            def public(self): self.public_only = True
            def _foreign(self): self.foreign_only = True
            @staticmethod
            def _static(self): self.static_only = True
            async def _async(self): self.async_only = True
        """,
    )
    attributes = {
        record
        for record in snapshot.splitlines()
        if record.startswith("attribute posthog.Client.")
    }
    assert attributes == {"attribute posthog.Client.visible = True"}


def test_direct_constructor_records_and_griffe_branch_precedence_are_preserved(
    tmp_path,
):
    snapshot = _snapshot_for_source(
        tmp_path,
        """
        class Client:
            def __init__(self):
                self.direct: int = 10
                self._configure()
            def _configure(self):
                self.direct = 20
                if condition:
                    self.branch = "first"
                else:
                    self.branch = "second"
                self.root = None
                if self.root is None:
                    try:
                        self.root = getcwd()
                    except Exception:
                        self.root = None
        """,
    )
    assert "attribute posthog.Client.direct: int = 10\n" in snapshot
    assert "attribute posthog.Client.branch = 'first'\n" in snapshot
    assert "attribute posthog.Client.root = getcwd()\n" in snapshot


def test_helper_attribute_changes_remain_visible_in_snapshot(tmp_path):
    source = """
        class Client:
            def __init__(self): self._configure()
            def _configure(self): self.visible = True
    """
    before = _snapshot_for_source(tmp_path, source)
    removed = _snapshot_for_source(
        tmp_path, source.replace("self.visible = True", "pass")
    )
    changed = _snapshot_for_source(
        tmp_path, source.replace("self.visible = True", "self.visible = False")
    )
    assert "attribute posthog.Client.visible = True\n" in before
    assert "attribute posthog.Client.visible" not in removed
    assert "attribute posthog.Client.visible = False\n" in changed


def test_constructor_discovery_retains_builtin_dataclass_processing(tmp_path):
    snapshot = _snapshot_for_source(
        tmp_path,
        """
        from dataclasses import dataclass
        @dataclass
        class Record:
            value: str
        """,
    )
    assert "class posthog.Record(value: str)\n" in snapshot


def test_constructor_helper_discovery_restores_visitor_after_failure(monkeypatch):
    import ast
    import griffe
    from constructor_helper_attributes import ConstructorHelperAttributes

    cls = griffe.Class("Client")
    constructor = griffe.Function("__init__", parent=cls)
    cls.set_member("__init__", constructor)
    source = (
        "class Client:\n"
        " def __init__(self): self._helper()\n"
        " def _helper(self): self.value = 1\n"
    )
    agent = griffe.Visitor("sample", Path("sample.py"), source, griffe.Extensions())
    agent.current = cls

    def fail_to_visit(node):
        raise RuntimeError("sentinel")

    monkeypatch.setattr(agent, "visit", fail_to_visit)
    with pytest.raises(RuntimeError, match="sentinel"):
        ConstructorHelperAttributes().on_class_members(
            node=ast.parse(source).body[0], cls=cls, agent=agent
        )
    assert agent.current is cls


def main() -> int:
    return pytest.main([str(Path(__file__)), "--timeout=30", "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
