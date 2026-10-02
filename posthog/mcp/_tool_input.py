"""Describe tool arguments by field name without reading their values."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .constants import PostHogMCPAnalyticsProperty
from .types import InputAliasMap, JsonRecord, ToolInputOptions

_MAX_INPUT_KEYS = 20
_MAX_KEY_LENGTH = 64
_ANALYTICS_KEYS = {"context", "llm_model", "conversation_id"}


def _declared_properties(schema: Any) -> Dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    nodes = _root_reference_chain(schema)
    if nodes is None:
        return {}
    return {
        key: value
        for node in nodes
        if isinstance(node.get("properties"), dict)
        for key, value in node["properties"].items()
    }


def _root_reference_chain(schema: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    nodes = [schema]
    while len(nodes) <= 8:
        ref = nodes[-1].get("$ref")
        if ref is None:
            return nodes
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        target: Any = schema
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict) or any(target is node for node in nodes):
            return None
        nodes.append(target)
    return None


def get_registered_tool_input_schema(
    server: Any, name: str
) -> Optional[Dict[str, Any]]:
    try:
        schema = server._tool_manager.get_tool(name).parameters
    except Exception:  # noqa: BLE001 - analytics must not break the call
        return None
    return schema if isinstance(schema, dict) else None


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
    aliases: Optional[InputAliasMap],
    input_value: Dict[str, Any],
    can_record: Callable[[str], bool],
) -> List[str]:
    if not isinstance(aliases, dict):
        return []
    used = []
    for canonical, names in aliases.items():
        if (
            not isinstance(canonical, str)
            or canonical in input_value
            or len(canonical) > _MAX_KEY_LENGTH
            or not can_record(canonical)
        ):
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
        if alias and len(alias) <= _MAX_KEY_LENGTH and can_record(alias):
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
        recording_decisions: Dict[tuple[str, bool], bool] = {}

        def can_record(key: str, is_declared: bool) -> bool:
            cache_key = (key, is_declared)
            if cache_key in recording_decisions:
                return recording_decisions[cache_key]
            record = is_declared
            if resolved.should_record_input_key is not None:
                try:
                    record = (
                        resolved.should_record_input_key(key, {"declared": is_declared})
                        is True
                    )
                except Exception:  # noqa: BLE001 - analytics callbacks fail closed
                    record = False
            recording_decisions[cache_key] = record
            return record

        for key in keys:
            is_declared = key in known
            if len(key) <= _MAX_KEY_LENGTH and can_record(key, is_declared):
                (declared if is_declared else undeclared).append(key)
            else:
                has_redacted = True

        visible = [*sorted(declared), *sorted(undeclared)][:_MAX_INPUT_KEYS]
        if has_redacted and len(visible) < _MAX_INPUT_KEYS:
            visible.append("[redacted]")
        aliases_used = _aliases_used(
            resolved.input_aliases, input_value, lambda key: can_record(key, True)
        )
        result: JsonRecord = {
            PostHogMCPAnalyticsProperty.INPUT_KEYS: visible,
        }
        if aliases_used:
            result[PostHogMCPAnalyticsProperty.INPUT_ALIASES_USED] = aliases_used
        return result
    except Exception:  # noqa: BLE001 - analytics must not change tool dispatch
        return {}
