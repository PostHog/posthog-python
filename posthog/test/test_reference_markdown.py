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


def test_one_migration_guide_titles_the_page_with_its_h1():
    head, body = generator.migration_page(
        ["# Migrate the Python SDK from 7.x.x to 8.0.0\n\nIntro."]
    )
    assert 'title: "Migrate the Python SDK from 7.x.x to 8.0.0"' in head
    assert body == "Intro."


def test_several_migration_guides_keep_their_h1s():
    head, *guides = generator.migration_page(["# Guide 2\n\nB.", "# Guide 1\n\nA."])
    assert 'title: "PostHog Python SDK migration guides"' in head
    assert guides == ["# Guide 2\n\nB.", "# Guide 1\n\nA."]


@pytest.mark.parametrize("major, pin", [(7, '"posthog>=7,<8"'), (8, '"posthog>=8,<9"')])
def test_getting_started_pins_the_install_to_the_page_major(major, pin):
    text = generator.getting_started(major)
    assert f"pip install {pin}" in text
    assert "{major}" not in text and "{next_major}" not in text


def test_unannotated_returns_gets_a_placeholder_type(pkg):
    md = generator.entry(
        pkg["get_remote_config_payload"], "posthog.get_remote_config_payload"
    )
    assert "- | The payload associated" in md


def test_drf_helpers_are_in_utils():
    assert "integrations.drf.exception_handler" in generator.UTILS
    assert "integrations.drf.create_exception_handler" in generator.UTILS


def test_parameter_tables_fold_type_and_default_into_two_columns(pkg):
    cls = pkg["client.Client"]
    generator.clean(cls)
    md = generator.entry(cls.members["__init__"], "Client(...)")
    assert "Name | Description\n---- | -----------" in md
    assert "Name | Type | Description | Default" not in md
    assert "`project_api_key` | `str`. PostHog project API key/token. Required." in md
    assert (
        "`flag_definition_cache_provider` | `Optional[FlagDefinitionCacheProvider]`."
        in md
    )
    assert "Default: `None`." in md


def test_prose_paragraphs_unwrap_hard_line_breaks(pkg):
    cls = pkg["client.Client"]
    generator.clean(cls)
    md = generator.entry(cls.members["identify_context"], "Client.identify_context")
    assert (
        "uses the [contexts](/docs/libraries/python#posthognew_context) pattern rather than a separate `identify()` method."
        in md
    )
