"""Describe tool arguments by field name without reading their values."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .constants import PostHogMCPAnalyticsProperty
from .types import InputAliasMap, JsonRecord, ToolInputOptions

_MAX_INPUT_KEYS = 20
_MAX_KEY_LENGTH = 64
_ANALYTICS_KEYS = {"context", "llm_model", "conversation_id"}


def _declared_properties(schema: Any) -> Dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _alias_names(aliases: Optional[InputAliasMap]) -> List[str]:
    if not isinstance(aliases, dict):
        return []
    return [
        name
        for names in aliases.values()
        if isinstance(names, (list, tuple))
        for name in names
        if isinstance(name, str)
    ]


def _aliases_used(
    aliases: Optional[InputAliasMap], input_value: Dict[str, Any]
) -> List[str]:
    if not isinstance(aliases, dict):
        return []
    used = []
    for canonical, names in aliases.items():
        if not isinstance(canonical, str) or canonical in input_value:
            continue
        names_value: Any = names
        if not isinstance(names_value, (list, tuple)):
            continue
        alias = next(
            (
                name
                for name in names_value
                if isinstance(name, str) and name in input_value
            ),
            None,
        )
        if (
            alias
            and len(alias) <= _MAX_KEY_LENGTH
            and len(canonical) <= _MAX_KEY_LENGTH
        ):
            used.append(f"{alias}:{canonical}")
    return sorted(used)[:_MAX_INPUT_KEYS]


def get_tool_input_properties(
    input_value: Any,
    input_schema: Any = None,
    options: Optional[ToolInputOptions] = None,
) -> JsonRecord:
    """Describe tool arguments without reading their values.

    The schema and aliases must come from the server. Unknown names become one
    ``[redacted]`` entry because an argument name can contain private data.
    """
    try:
        if type(input_value) is not dict:
            return {}
        resolved = options or ToolInputOptions()
        properties = _declared_properties(input_schema)
        known = set(properties) | set(_alias_names(resolved.input_aliases))
        keys = [
            key
            for key in input_value
            if isinstance(key, str) and (key in known or key not in _ANALYTICS_KEYS)
        ]
        declared: List[str] = []
        undeclared: List[str] = []
        has_redacted = False
        for key in keys:
            is_declared = key in known
            record = is_declared
            if resolved.should_record_input_key is not None:
                try:
                    record = (
                        resolved.should_record_input_key(key, {"declared": is_declared})
                        is True
                    )
                except Exception:  # noqa: BLE001 - analytics callbacks fail closed
                    record = False
            if len(key) <= _MAX_KEY_LENGTH and record:
                (declared if is_declared else undeclared).append(key)
            else:
                has_redacted = True

        visible = [*sorted(declared), *sorted(undeclared)][:_MAX_INPUT_KEYS]
        if has_redacted and len(visible) < _MAX_INPUT_KEYS:
            visible.append("[redacted]")
        aliases_used = _aliases_used(resolved.input_aliases, input_value)
        result: JsonRecord = {
            PostHogMCPAnalyticsProperty.INPUT_KEYS: visible,
        }
        if aliases_used:
            result[PostHogMCPAnalyticsProperty.INPUT_ALIASES_USED] = aliases_used
        return result
    except Exception:  # noqa: BLE001 - analytics must not change tool dispatch
        return {}
