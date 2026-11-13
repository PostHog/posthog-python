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
        target = _resolve_local_pointer(schema, ref)
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
        if not _can_record_canonical(canonical, input_value, can_record):
            continue
        names_value: Any = names
        if not isinstance(names_value, (list, tuple)):
            continue
        alias = _first_present_alias(names_value, input_value)
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
        keys = _input_keys(input_value, known)
        recorder = _InputKeyRecorder(resolved)
        visible = _visible_input_keys(keys, known, recorder)
        aliases_used = _aliases_used(
            resolved.input_aliases,
            input_value,
            lambda key: recorder.can_record(key, True),
        )
        result: JsonRecord = {
            PostHogMCPAnalyticsProperty.INPUT_KEYS: visible,
        }
        if aliases_used:
            result[PostHogMCPAnalyticsProperty.INPUT_ALIASES_USED] = aliases_used
        return result
    except Exception:  # noqa: BLE001 - analytics must not change tool dispatch
        return {}


def _resolve_local_pointer(schema: Dict[str, Any], ref: str) -> Any:
    target: Any = schema
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")  # JSON Pointer escapes
        target = target.get(key) if isinstance(target, dict) else None
    return target


def _first_present_alias(names: Any, input_value: Dict[str, Any]) -> Optional[str]:
    return next(
        (name for name in names if isinstance(name, str) and name in input_value), None
    )


def _can_record_canonical(
    canonical: Any, input_value: Dict[str, Any], can_record: Callable[[str], bool]
) -> bool:
    return (
        isinstance(canonical, str)
        and canonical not in input_value
        and len(canonical) <= _MAX_KEY_LENGTH
        and can_record(canonical)
    )


def _visible_input_keys(
    keys: List[str], known: set, recorder: _InputKeyRecorder
) -> List[str]:
    declared: List[str] = []
    undeclared: List[str] = []
    has_redacted = False
    for key in keys:
        is_declared = key in known
        if len(key) <= _MAX_KEY_LENGTH and recorder.can_record(key, is_declared):
            (declared if is_declared else undeclared).append(key)
        else:
            has_redacted = True

    visible = [*sorted(declared), *sorted(undeclared)][:_MAX_INPUT_KEYS]
    if has_redacted and len(visible) < _MAX_INPUT_KEYS:
        visible.append("[redacted]")
    return visible


def _input_keys(input_value: Dict[str, Any], known: set) -> List[str]:
    return [
        key
        for key in input_value
        if isinstance(key, str) and (key in known or key not in _ANALYTICS_KEYS)
    ]


class _InputKeyRecorder:
    def __init__(self, options: ToolInputOptions) -> None:
        self.options = options
        self.decisions: Dict[tuple[str, bool], bool] = {}

    def can_record(self, key: str, is_declared: bool) -> bool:
        cache_key = (key, is_declared)
        if cache_key in self.decisions:
            return self.decisions[cache_key]
        record = is_declared
        if self.options.should_record_input_key is not None:
            try:
                record = (
                    self.options.should_record_input_key(key, {"declared": is_declared})
                    is True
                )
            except Exception:  # noqa: BLE001 - analytics callbacks fail closed
                record = False
        self.decisions[cache_key] = record
        return record
