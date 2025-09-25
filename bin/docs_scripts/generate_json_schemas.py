#!/usr/bin/env python3
"""
Generate comprehensive SDK documentation JSON from PostHog Python SDK.
This script inspects the code and docstrings to create documentation in the specified format.
"""

import json
import inspect
import re
from dataclasses import is_dataclass, fields
from typing import get_origin, get_args, Union
from textwrap import dedent
from doc_constant import (
    DOCUMENTATION_METADATA,
    DOCSTRING_PATTERNS,
    OUTPUT_CONFIG,
    DOC_DEFAULTS,
)
import os


def extract_examples_from_docstring(docstring: str) -> list:
    """Extract code examples from docstring."""
    if not docstring:
        return []

    examples = []

    # Look for Examples section in the new format
    examples_section_match = re.search(
        DOCSTRING_PATTERNS["examples_section"], docstring, re.DOTALL
    )
    if examples_section_match:
        examples_content = examples_section_match.group(1).strip()
        # Extract code blocks from the Examples section
        code_blocks = re.findall(
            DOCSTRING_PATTERNS["code_block"], examples_content, re.DOTALL
        )
        for i, code_block in enumerate(code_blocks):
            # Remove common leading whitespace while preserving relative indentation
            code = dedent(code_block).strip()

            # Extract name from first comment line if present
            lines = code.split("\n")
            name = f"Example {i + 1}"  # Default fallback

            if lines and lines[0].strip().startswith("#"):
                # Extract name from first comment, keep the comment in the code
                comment_text = lines[0].strip()[1:].strip()
                if comment_text:
                    name = comment_text

            examples.append({"id": f"example_{i + 1}", "name": name, "code": code})

    return examples


def extract_details_from_docstring(docstring: str) -> str:
    """Extract details section from docstring."""
    if not docstring:
        return ""

    # Look for Details section
    details_match = re.search(
        DOCSTRING_PATTERNS["details_section"], docstring, re.DOTALL
    )
    if details_match:
        details_content = details_match.group(1).strip()
        # Clean up formatting
        return details_content.replace("\n", " ")

    return ""


def parse_docstring_tags(docstring: str) -> dict:
    """Parse tags from docstring Category section."""
    if not docstring:
        return {}

    tags = {}

    # Extract Category section
    category_match = re.search(DOCSTRING_PATTERNS["category_section"], docstring)
    if category_match:
        category_value = category_match.group(1).strip()
        tags["category"] = category_value

    return tags


def extract_description_from_docstring(docstring: str) -> str:
    """Extract main description from docstring."""
    if not docstring:
        return ""

    # Clean up the docstring
    cleaned = dedent(docstring).strip()

    # Find the end of the description by looking for first section marker
    # Check for Args:, Examples:, Details:, or Category: sections
    section_patterns = [
        DOCSTRING_PATTERNS["args_marker"],
        DOCSTRING_PATTERNS["examples_marker"],
        DOCSTRING_PATTERNS["details_marker"],
        DOCSTRING_PATTERNS["category_marker"],
    ]

    end_pos = len(cleaned)
    for pattern in section_patterns:
        match = re.search(pattern, cleaned)
        if match:
            end_pos = min(end_pos, match.start())

    # Extract description up to the first section marker
    description = cleaned[:end_pos].strip()

    # Remove one level of \n since it will be rendered as markdown
    # and \n will be padded in later steps
    description = description.replace("\n", " ")

    return description


def get_type_name(type_annotation) -> str:
    """Convert type annotation to string name."""
    if type_annotation is None or type_annotation is type(None):
        return "any"

    # Handle typing constructs
    origin = get_origin(type_annotation)
    if origin is not None:
        # Handle Union types (including Optional)
        if origin is Union:
            args = get_args(type_annotation)
            if len(args) == 2 and type(None) in args:
                # This is Optional[Type] - get the non-None type
                non_none_type = next(arg for arg in args if arg is not type(None))
                return f"Optional[{get_type_name(non_none_type)}]"
            else:
                # Regular Union - list all types
                type_names = [get_type_name(arg) for arg in args]
                return f"Union[{', '.join(type_names)}]"

        # Handle other generic types (List, Dict, etc.)
        origin_name = getattr(origin, "__name__", str(origin))
        args = get_args(type_annotation)
        if args:
            arg_names = [get_type_name(arg) for arg in args]
            return f"{origin_name}[{', '.join(arg_names)}]"
        else:
            return origin_name

    # Handle regular types
    elif hasattr(type_annotation, "__name__"):
        return type_annotation.__name__
    else:
        return str(type_annotation)


def analyze_parameter(param: inspect.Parameter, docstring: str = "") -> dict:
    """Analyze a function parameter and return its documentation."""
    # Determine if parameter is optional (has default value)
    is_optional = param.default == inspect.Parameter.empty

    # Get the type annotation
    type_annotation = param.annotation
    param_type = "any"

    if type_annotation != inspect.Parameter.empty:
        # Handle Union/Optional types first
        origin = get_origin(type_annotation)
        if origin is Union:
            args = get_args(type_annotation)
            if len(args) == 2 and type(None) in args:
                # This is Optional[Type]
                non_none_type = next(arg for arg in args if arg is not type(None))
                param_type = get_type_name(non_none_type)
                is_optional = True
            else:
                # Other Union types, use first type
                param_type = get_type_name(args[0]) if args else "any"
        else:
            param_type = get_type_name(type_annotation)
    elif param.default != inspect.Parameter.empty:
        # No type annotation, but has default value - infer type from default
        param_type = get_type_name(type(param.default))

    # Extract parameter description from Args section
    param_description = ""
    if docstring:
        # Look for Args section and extract description for this parameter
        args_section_match = re.search(
            DOCSTRING_PATTERNS["args_section"], docstring, re.DOTALL
        )
        if args_section_match:
            args_content = args_section_match.group(1)
            # Look for the parameter description
            param_pattern = DOCSTRING_PATTERNS["param_description"].format(
                param_name=re.escape(param.name)
            )
            param_match = re.search(
                param_pattern, args_content, re.MULTILINE | re.DOTALL
            )
            if param_match:
                param_description = param_match.group(1).strip().replace("\n", " ")

    param_info = {
        "name": param.name,
        "description": param_description,
        "isOptional": is_optional,
        "type": param_type,
    }

    return param_info


def analyze_function(func, name: str) -> dict:
    """Analyze a function and return its documentation."""
    try:
        sig = inspect.signature(func)
        docstring = inspect.getdoc(func) or ""

        # Skip functions with empty docstrings
        if not docstring.strip():
            return {}

        # Extract parameters (excluding 'self')
        params = []
        for param_name, param in sig.parameters.items():
            if param_name != "self":
                params.append(analyze_parameter(param, docstring))

        # Special handling for constructor
        display_name = name
        if name == "__init__":
            display_name = func.__qualname__.split(".")[0]

        # Parse tags from docstring
        tags = parse_docstring_tags(docstring)

        category = tags.get("category", None)

        # Extract description
        description = extract_description_from_docstring(docstring)

        # Skip if no meaningful description
        if not description.strip():
            return {}

        # Extract details section (only if it exists)
        details = extract_details_from_docstring(docstring)

        # Get examples from docstring, do not generate fallback examples
        examples = extract_examples_from_docstring(docstring)
        # If no examples, do not include the examples key or set to empty list

        result = {
            "id": name,
            "title": display_name,
            "description": description,
            "details": details,
            "category": category,
            "params": params,
            "showDocs": DOC_DEFAULTS["showDocs"],
            "releaseTag": DOC_DEFAULTS["releaseTag"],
            "returnType": {
                "id": "return_type",
                "name": get_type_name(sig.return_annotation)
                if sig.return_annotation != inspect.Signature.empty
                else DOC_DEFAULTS["return_type_void"],
            },
        }
        if examples:
            result["examples"] = examples
        return result
    except Exception as e:
        print(f"Error analyzing function {name}: {e}")
        return {}


def analyze_class(cls) -> dict:
    """Analyze a class and return its documentation."""
    class_doc = inspect.getdoc(cls) or f"Class: {cls.__name__}"

    # Get all public methods and constructor
    functions = []
    for method_name in dir(cls):
        if method_name.startswith("_") and method_name != "__init__":
            continue

        method = getattr(cls, method_name)
        if callable(method):
            func_info = analyze_function(method, method_name)
            if func_info:  # Only add if not None (empty docstring check)
                functions.append(func_info)

    return {
        "id": cls.__name__,
        "title": cls.__name__,
        "description": extract_description_from_docstring(class_doc),
        "functions": functions,
    }


def analyze_type(cls) -> dict:
    """Analyze a type/dataclass and return its documentation."""
    type_info = {
        "id": cls.__name__,
        "name": cls.__name__,
        "path": f"{cls.__module__}.{cls.__name__}",
        "properties": [],
        "example": "",
    }

    if is_dataclass(cls):
        # Handle dataclass
        for field in fields(cls):
            prop = {
                "name": field.name,
                "type": get_type_name(field.type),
                "description": f"Field: {field.name}",
            }
            type_info["properties"].append(prop)
    elif hasattr(cls, "__annotations__"):
        # Handle TypedDict or annotated class
        for field_name, field_type in cls.__annotations__.items():
            prop = {
                "name": field_name,
                "type": get_type_name(field_type),
                "description": f"Field: {field_name}",
            }
            type_info["properties"].append(prop)

    return type_info


def generate_sdk_documentation():
    """Generate complete SDK documentation in the requested format."""

    # Import PostHog components
    import posthog
    from posthog.client import Client
    import posthog.types as types_module
    import posthog.args as args_module
    from posthog.version import VERSION

    # Main SDK info
    sdk_info = {
        "version": VERSION,
        "id": "posthog-python",
        "title": "PostHog Python SDK",
        "description": "Integrate PostHog into any python application.",
        "slugPrefix": DOCUMENTATION_METADATA["slugPrefix"],
        "specUrl": DOCUMENTATION_METADATA["specUrl"],
    }

    # Collect types
    types_list = []

    # Types from posthog.types
    for name in dir(types_module):
        obj = getattr(types_module, name)
        if inspect.isclass(obj) and not name.startswith("_"):
            try:
                type_info = analyze_type(obj)
                types_list.append(type_info)
            except Exception as e:
                print(f"Error analyzing type {name}: {e}")

    # Types from posthog.args
    for name in dir(args_module):
        obj = getattr(args_module, name)
        if inspect.isclass(obj) and not name.startswith("_"):
            try:
                type_info = analyze_type(obj)
                types_list.append(type_info)
            except Exception as e:
                print(f"Error analyzing type {name}: {e}")

    # Clean types of empty types

    # Remove types that have no properties and no examples
    # Remove types that have no properties and no examples
    types_list = [
        t for t in types_list if len(t["properties"]) > 0 or t["example"] != ""
    ]

    # Collect classes
    classes_list = []

    # Main PostHog class (renamed from Client)
    client_class = analyze_class(Client)
    client_class["id"] = "PostHog"
    client_class["title"] = "PostHog"
    classes_list.append(client_class)

    # Global module functions (functions callable as posthog.function_name)
    global_functions = []
    for func_name in dir(posthog):
        # Skip private functions and non-callables
        if func_name.startswith("_") or not callable(getattr(posthog, func_name)):
            continue

        func = getattr(posthog, func_name)
        # Only include functions actually defined in the posthog module (not imported)
        # and exclude class references
        if (
            func_name not in ["Client", "Posthog"]
            and hasattr(func, "__module__")
            and func.__module__ == "posthog"
        ):
            try:
                func_info = analyze_function(func, func_name)
                if func_info:  # Only add if not None (has proper docstring)
                    global_functions.append(func_info)
            except Exception:
                continue

    # Add global functions as a "class"
    if global_functions:
        classes_list.append(
            {
                "id": "PostHogModule",
                "title": "PostHog Module Functions",
                "description": "Global functions available in the PostHog module",
                "functions": global_functions,
            }
        )

    # Collect categories from functions
    categories = ["Initialization", "Identification", "Capture"]
    seen_categories = set(categories)
    for class_info in classes_list:
        if "functions" in class_info:
            for func in class_info["functions"]:
                if (
                    "category" in func
                    and func["category"] not in seen_categories
                    and func["category"]
                ):
                    categories.append(func["category"])
                    seen_categories.add(func["category"])

    # Create the final structure
    result = {
        "id": "posthog-python",
        "hogRef": DOCUMENTATION_METADATA["hogRef"],
        "info": sdk_info,
        "types": types_list,
        "classes": classes_list,
        "categories": categories,
    }

    return result


if __name__ == "__main__":
    print("Generating PostHog Python SDK documentation...")

    try:
        documentation = generate_sdk_documentation()

        output_file = os.path.join(
            str(OUTPUT_CONFIG["output_dir"]), str(OUTPUT_CONFIG["filename"])
        )
        output_file_latest = os.path.join(
            str(OUTPUT_CONFIG["output_dir"]), str(OUTPUT_CONFIG["filename_latest"])
        )

        # Write to current version
        with open(output_file, "w") as f:
            json.dump(documentation, f, indent=int(OUTPUT_CONFIG["indent"]))
        # Write to latest
        with open(output_file_latest, "w") as f:
            json.dump(documentation, f, indent=int(OUTPUT_CONFIG["indent"]))

        print(f"✓ Generated {output_file}")

        # Print summary
        types_count = len(documentation["types"])
        classes_count = len(documentation["classes"])

        total_functions = sum(len(cls["functions"]) for cls in documentation["classes"])

        print("📊 Documentation Summary:")
        print(f"   • {types_count} types documented")
        print(f"   • {classes_count} classes documented")
        print(f"   • {total_functions} functions documented")

    except Exception as e:
        print(f"❌ Error generating documentation: {e}")
        import traceback

        traceback.print_exc()
