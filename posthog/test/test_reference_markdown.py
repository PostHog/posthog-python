"""Checks the Markdown reference generator's signatures, which griffe2md's own template got wrong."""

import importlib.util
from pathlib import Path

import pytest

griffe = pytest.importorskip("griffe")
pytest.importorskip("griffe2md")

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "generate_markdown", ROOT / "bin/docs_scripts/generate_markdown.py"
)
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


@pytest.fixture(scope="module")
def pkg():
    return griffe.load(
        "posthog",
        search_paths=[str(ROOT)],
        docstring_parser="google",
        docstring_options={"returns_multiple_items": False},
        resolve_aliases=False,
    )


def declaration(pkg, path: str) -> str:
    """The first line of the entry's code block."""
    md = generator.entry(pkg[path], path)
    return md.split("```python\n", 1)[1].split("\n", 1)[0]


def test_unannotated_parameters_get_no_annotation(pkg):
    line = declaration(pkg, "client.Client.__init__")
    assert "debug=False" in line
    assert "debug: str" not in line
    assert "_use_ai_lane" not in line, "private parameters stay out of the signature"


def test_coroutines_are_marked_async(pkg):
    assert declaration(pkg, "async_client.AsyncClient.flush").startswith("async ")
    assert not declaration(pkg, "async_client.AsyncClient.capture").startswith("async ")


def test_keyword_only_marker_is_kept(pkg):
    assert ", *, groups" in declaration(pkg, "client.Client.evaluate_flags")


def test_constructor_is_called_by_its_class_name(pkg):
    assert declaration(pkg, "client.Client.__init__").startswith("Client(")


def test_a_wrapped_returns_description_is_one_row(pkg):
    md = generator.entry(
        pkg["client.Client.get_feature_flag_payload"], "Client.get_feature_flag_payload"
    )
    assert md.count("`Optional[object]` |") == 1


def test_summaries_keep_the_whole_first_paragraph(pkg):
    cls = pkg["feature_flag_evaluations.FeatureFlagEvaluations"]
    generator.clean(cls)
    md = generator.entry(cls, "FeatureFlagEvaluations")
    assert "on the first access per (distinct_id, flag, value) tuple" in md


@pytest.mark.parametrize("major, pin", [(7, '"posthog>=7,<8"'), (8, '"posthog>=8,<9"')])
def test_getting_started_pins_the_install_to_the_page_major(major, pin):
    text = generator.getting_started(major)
    assert f"pip install {pin}" in text
    assert "{major}" not in text and "{next_major}" not in text
