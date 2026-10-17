"""A bounded JSON Schema subset for synchronous tool argument evaluation.

No pattern matching, recursive reference graph, or quadratic uniqueness checks
are admitted. An expanded schema tree has at most 256 nodes; an argument tree
has at most 1024 nodes. Their product may not exceed 4096, bounding validation
work and the error trees collected by combinators. JSON Schema supplies semantics.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

MAX_TOOL_JSON_BYTES = 65536
MAX_TOOL_VALUE_NODES = 1024
MAX_TOOL_SCHEMA_NODES = 2048
MAX_TOOL_EXPANDED_SCHEMAS = 256
MAX_TOOL_JSON_DEPTH = 32
MAX_TOOL_EVALUATION_WORK = 4096


class ToolSchemaBudgetExceeded(ValueError):
    """Evaluation cannot establish correctness within the documented bounds."""


def bounded_json_tree(value: Any, *, schema: bool = False) -> int:
    limit = MAX_TOOL_SCHEMA_NODES if schema else MAX_TOOL_VALUE_NODES
    pending, nodes = [(value, 0)], 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > limit or depth > MAX_TOOL_JSON_DEPTH:
            raise ToolSchemaBudgetExceeded("tool JSON exceeds the evaluation node/depth limit")
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("tool JSON must contain finite JSON values") from error
    if size > MAX_TOOL_JSON_BYTES:
        raise ToolSchemaBudgetExceeded("tool JSON exceeds the evaluation byte limit")
    return nodes


_ANNOTATIONS = {"title", "description", "default", "examples", "$comment", "deprecated", "readOnly", "writeOnly"}
_ASSERTIONS = {"type", "enum", "const", "required", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"}
_SINGLE = {"additionalProperties", "items", "additionalItems"}
_ARRAY = {"anyOf", "oneOf", "allOf", "prefixItems"}
_MAPPING = {"properties", "$defs", "definitions"}
_ALLOWED = _ANNOTATIONS | _ASSERTIONS | _SINGLE | _ARRAY | _MAPPING | {"$ref", "$schema"}
_DIALECTS = {
    f"{scheme}://json-schema.org/{version}"
    for scheme in ("http", "https")
    for version in ("draft-04/schema", "draft-06/schema", "draft-07/schema", "draft/2019-09/schema", "draft/2020-12/schema")
}


def _check_schema_graph(schema: dict[str, Any]) -> int:
    bounded_json_tree(schema, schema=True)
    expanded = 0

    def reference_target(reference):
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise ValueError("tool schema references must be local; no downloads")
        fragment = unquote(reference[1:])
        if fragment and not fragment.startswith("/"):
            raise ValueError("tool schema references require local JSON pointers")
        current = schema
        try:
            for part in fragment.split("/")[1:]:
                part = part.replace("~1", "/").replace("~0", "~")
                current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ValueError("tool schema contains an unresolved local reference") from error
        return current

    def visit(current, ancestors, depth):
        nonlocal expanded
        expanded += 1
        if expanded > MAX_TOOL_EXPANDED_SCHEMAS or depth > MAX_TOOL_JSON_DEPTH:
            raise ToolSchemaBudgetExceeded("tool schema exceeds the expanded node/depth limit")
        if isinstance(current, bool):
            return
        if not isinstance(current, dict):
            raise ValueError("tool schemas must be objects or booleans")
        identity = id(current)
        if identity in ancestors:
            raise ValueError("recursive tool schema references are unsupported")
        if set(current) - _ALLOWED:
            raise ValueError("unsupported tool schema keyword; use the documented bounded schema subset")
        if "$schema" in current and (not isinstance(current["$schema"], str)
                                     or current["$schema"].rstrip("#") not in _DIALECTS):
            raise ValueError("unsupported tool schema dialect")
        ancestors = ancestors | {identity}
        if "$ref" in current:
            visit(reference_target(current["$ref"]), ancestors, depth + 1)
        for key in _MAPPING & current.keys():
            if not isinstance(current[key], dict):
                raise ValueError("tool schema mappings must be objects")
            for child in current[key].values():
                visit(child, ancestors, depth + 1)
        for key in _ARRAY & current.keys():
            if not isinstance(current[key], list):
                raise ValueError("tool schema branches must be arrays")
            for child in current[key]:
                visit(child, ancestors, depth + 1)
        for key in _SINGLE & current.keys():
            children = current[key] if key == "items" and isinstance(current[key], list) else [current[key]]
            for child in children:
                visit(child, ancestors, depth + 1)

    visit(schema, set(), 0)
    return expanded


def bounded_tool_schema(schema: dict[str, Any], *, instance: Any = None):
    """Reject expensive constructs before even running meta-schema validation."""
    expanded = _check_schema_graph(schema)
    if instance is not None and expanded * bounded_json_tree(instance) > MAX_TOOL_EVALUATION_WORK:
        raise ToolSchemaBudgetExceeded("tool schema and arguments exceed the combined evaluation work limit")
    from giraffe.models import validated_json_schema

    return validated_json_schema(schema)


def tool_call_fingerprint(name: str, arguments: dict) -> str:
    import hashlib

    bounded_json_tree(arguments)

    def canonical(value):
        if isinstance(value, dict):
            return ["object", [[key, canonical(item)] for key, item in sorted(value.items())]]
        if isinstance(value, list):
            return ["array", [canonical(item) for item in value]]
        if isinstance(value, bool):
            return ["boolean", value]
        if isinstance(value, (int, float)):
            numerator, denominator = value.as_integer_ratio() if isinstance(value, float) else (value, 1)
            return ["number", str(numerator), str(denominator)]
        return ["null"] if value is None else ["string", value]

    return hashlib.sha256(json.dumps([name, canonical(arguments)], ensure_ascii=False).encode()).hexdigest()
