#!/usr/bin/env python3
"""
Constants for PostHog Python SDK documentation generation.
"""

# Types that are built-in to Python and don't need to be documented
NO_DOCS_TYPES = [
    "Client",
    "any",
    "int",
    "float",
    "bool",
    "dict",
    "list",
    "str",
    "tuple",
    "set",
    "frozenset",
    "bytes",
    "bytearray",
    "memoryview",
    "range",
    "slice",
    "complex",
    "Union",
    "Optional",
    "Any",
    "Callable",
    "Type",
    "TypeVar",
    "Generic",
    "Literal",
    "ClassVar",
    "Final",
    "Annotated",
    "NotRequired",
    "Required",
    "void",
    "None",
    "NoneType",
    "object",
    "Unpack",
    "BaseException",
    "Exception",
]

# Documentation generation metadata
DOCUMENTATION_METADATA = {
    "hogRef": "0.1",
    "slugPrefix": "posthog-python",
    "specUrl": "https://github.com/PostHog/posthog-python"
}

# Docstring parsing patterns for new format
DOCSTRING_PATTERNS = {
    "examples_section": r'Examples:\s*\n(.*?)(?=\n\s*\n\s*Category:|\Z)',
    "args_section": r'Args:\s*\n(.*?)(?=\n\s*\n\s*Examples:|\n\s*\n\s*Details:|\n\s*\n\s*Category:|\Z)',
    "details_section": r'Details:\s*\n(.*?)(?=\n\s*\n\s*Examples:|\n\s*\n\s*Category:|\Z)',
    "category_section": r'Category:\s*\n\s*(.+?)\s*(?:\n|$)',
    "code_block": r'```(?:python)?\n(.*?)```',
    "param_description": r'^\s*{param_name}:\s*(.+?)(?=\n\s*\w+:|\Z)',
    "args_marker": r'\n\s*Args:\s*\n',
    "examples_marker": r'\n\s*Examples:\s*\n',
    "details_marker": r'\n\s*Details:\s*\n',
    "category_marker": r'\n\s*Category:\s*\n'
}

# Output file configuration
OUTPUT_CONFIG = {
    "filename": "posthog-python-references.json",
    "indent": 2
}

# Documentation structure defaults
DOC_DEFAULTS = {
    "showDocs": True,
    "releaseTag": "public",
    "return_type_void": "void",
    "max_optional_params": 3
}