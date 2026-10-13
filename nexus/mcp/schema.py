# nexus/mcp/schema.py
"""Validates tool arguments against the small part of JSON Schema our tools use.

A dependency-free subset on purpose (CLAUDE.md: reduce external dependencies): ``type`` (object,
string, integer, number, boolean, array), ``properties``, ``required``, ``additionalProperties:
false``, ``enum``, ``minimum``/``maximum``, ``minLength``/``maxLength``, ``pattern``, ``items``,
``maxItems`` and ``default``. ``tests/test_mcp_tools.py`` checks that every tool schema uses
nothing outside this list, so a schema can never claim a rule this validator silently ignores.

Arguments come from a language model, so every failure names the field and says what was wrong in
words the model can act on.
"""
from __future__ import annotations

import re

SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "enum", "minimum", "maximum",
    "minLength", "maxLength", "pattern", "items", "maxItems", "default", "description", "title",
})


class SchemaError(ValueError):
    """The arguments do not match the tool's schema. The message is shown to the model."""


def _type_ok(kind: str, value: object) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return True


def _check(name: str, rule: dict, value: object) -> None:
    kind = rule.get("type")
    if kind and not _type_ok(kind, value):
        raise SchemaError(f"'{name}' must be {'an' if kind in ('integer', 'object', 'array') else 'a'} "
                          f"{kind}")
    if "enum" in rule and value not in rule["enum"]:
        raise SchemaError(f"'{name}' must be one of: {', '.join(map(str, rule['enum']))}")
    if isinstance(value, str):
        if "minLength" in rule and len(value) < rule["minLength"]:
            raise SchemaError(f"'{name}' must be at least {rule['minLength']} characters")
        if "maxLength" in rule and len(value) > rule["maxLength"]:
            raise SchemaError(f"'{name}' must be at most {rule['maxLength']} characters")
        if "pattern" in rule and not re.search(rule["pattern"], value):
            raise SchemaError(f"'{name}' is not in the expected format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            raise SchemaError(f"'{name}' must be at least {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            raise SchemaError(f"'{name}' must be at most {rule['maximum']}")
    if isinstance(value, list):
        if "maxItems" in rule and len(value) > rule["maxItems"]:
            raise SchemaError(f"'{name}' may hold at most {rule['maxItems']} items")
        item_rule = rule.get("items")
        if isinstance(item_rule, dict):
            for i, item in enumerate(value):
                _check(f"{name}[{i}]", item_rule, item)


def validate(schema: dict, args: object) -> dict:
    """The arguments with defaults applied, or :class:`SchemaError`."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise SchemaError("arguments must be an object")
    props: dict = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        extra = sorted(set(args) - set(props))
        if extra:
            raise SchemaError(f"unknown argument '{extra[0]}'")
    for required in schema.get("required") or []:
        if args.get(required) is None:
            raise SchemaError(f"'{required}' is required")
    out = dict(args)
    for name, rule in props.items():
        if out.get(name) is None:
            if "default" in rule:
                out[name] = rule["default"]
            else:
                out.pop(name, None)
            continue
        _check(name, rule, out[name])
    return out
