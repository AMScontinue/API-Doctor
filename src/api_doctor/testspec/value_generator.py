"""Deterministic JSON Schema value generation for request baselines."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import ipaddress
import re
from typing import Any
from uuid import UUID


class ValueGenerationError(ValueError):
    """A schema lacks enough supported information to generate a safe value."""


def _merge_all_of(schema: dict[str, Any]) -> dict[str, Any]:
    merged = {key: deepcopy(value) for key, value in schema.items() if key != "allOf"}
    for item in schema.get("allOf", []):
        if not isinstance(item, dict):
            raise ValueGenerationError("allOf entries must be objects")
        item = _merge_all_of(item)
        for key, value in item.items():
            if key == "properties":
                merged.setdefault("properties", {}).update(deepcopy(value))
            elif key == "required":
                merged["required"] = list(dict.fromkeys([*merged.get("required", []), *value]))
            else:
                merged[key] = deepcopy(value)
    return merged


def _schema_type(schema: dict[str, Any]) -> str | None:
    value = schema.get("type")
    if isinstance(value, list):
        return next((item for item in value if item != "null"), None)
    if isinstance(value, str):
        return value
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def _string_value(schema: dict[str, Any], name: str, seed: int, anchor: datetime) -> str:
    field_format = schema.get("format")
    suffix = abs(seed) % 1_000_000
    formats = {
        "date-time": anchor.isoformat().replace("+00:00", "Z"),
        "date": anchor.date().isoformat(),
        "email": f"{re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-') or 'value'}-{suffix}@example.test",
        "uuid": str(UUID(int=abs(seed) % (1 << 128))),
        "uri": f"https://example.test/{suffix}",
        "ipv4": str(ipaddress.IPv4Address(0xC0000201 + suffix % 200)),
        "ipv6": str(ipaddress.IPv6Address(0x20010DB8000000000000000000000001 + suffix)),
    }
    if field_format in formats:
        value = formats[field_format]
    elif schema.get("pattern"):
        raise ValueGenerationError(
            f"cannot safely synthesize string for pattern {schema['pattern']!r}; provide example or default"
        )
    else:
        value = f"test-{name}-{suffix}"
    minimum = int(schema.get("minLength", 0))
    maximum = schema.get("maxLength")
    if len(value) < minimum:
        value = value.ljust(minimum, "x")
    if isinstance(maximum, int) and len(value) > maximum:
        plain = "x" * max(minimum, 1)
        if len(plain) > maximum:
            raise ValueGenerationError("minLength exceeds maxLength")
        value = plain[:maximum]
    return value


def _number_value(schema: dict[str, Any], integer: bool) -> int | float:
    minimum = schema.get("minimum", 0)
    exclusive = schema.get("exclusiveMinimum")
    if isinstance(exclusive, (int, float)) and not isinstance(exclusive, bool):
        minimum = exclusive + (1 if integer else 0.5)
    elif exclusive is True:
        minimum = minimum + (1 if integer else 0.5)
    value: int | float = int(minimum) if integer else float(minimum)
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        quotient = int(value / multiple)
        if quotient * multiple < value:
            quotient += 1
        value = quotient * multiple
        if integer:
            value = int(value)
    maximum = schema.get("maximum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(exclusive_maximum, (int, float)) and not isinstance(exclusive_maximum, bool):
        if value >= exclusive_maximum:
            raise ValueGenerationError("numeric schema has no supported value between its bounds")
    elif exclusive_maximum is True and maximum is not None and value >= maximum:
        raise ValueGenerationError("numeric schema has no supported value between its bounds")
    elif maximum is not None and value > maximum:
        raise ValueGenerationError("numeric minimum exceeds maximum")
    return value


def generate_value(
    schema: dict[str, Any],
    *,
    name: str,
    seed: int,
    anchor: datetime,
) -> Any:
    """Generate one reproducible value using contract evidence only."""
    schema = _merge_all_of(schema)
    if "example" in schema:
        return deepcopy(schema["example"])
    if "examples" in schema and isinstance(schema["examples"], list) and schema["examples"]:
        return deepcopy(schema["examples"][0])
    if "default" in schema:
        return deepcopy(schema["default"])
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return deepcopy(schema["enum"][0])
    if "const" in schema:
        return deepcopy(schema["const"])
    for combinator in ("oneOf", "anyOf"):
        choices = schema.get(combinator)
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            return generate_value(choices[0], name=name, seed=seed, anchor=anchor)

    value_type = _schema_type(schema)
    if value_type == "string":
        return _string_value(schema, name, seed, anchor)
    if value_type == "integer":
        return _number_value(schema, integer=True)
    if value_type == "number":
        return _number_value(schema, integer=False)
    if value_type == "boolean":
        return True
    if value_type == "null":
        return None
    if value_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueGenerationError(f"object {name!r} has invalid properties")
        result: dict[str, Any] = {}
        for index, field in enumerate(schema.get("required", [])):
            field_schema = properties.get(field)
            if not isinstance(field_schema, dict):
                raise ValueGenerationError(f"required field {field!r} has no schema")
            result[field] = generate_value(
                field_schema,
                name=field,
                seed=seed + index + 1,
                anchor=anchor,
            )
        return result
    if value_type == "array":
        item_schema = schema.get("items", {})
        if not isinstance(item_schema, dict):
            raise ValueGenerationError(f"array {name!r} has unsupported items")
        count = max(int(schema.get("minItems", 0)), 1)
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and count > maximum:
            raise ValueGenerationError("minItems exceeds maxItems")
        values = [
            generate_value(item_schema, name=f"{name}-{index}", seed=seed + index + 1, anchor=anchor)
            for index in range(count)
        ]
        if schema.get("uniqueItems") and len({repr(value) for value in values}) != len(values):
            raise ValueGenerationError(f"cannot generate unique items for {name!r}")
        return values
    raise ValueGenerationError(f"cannot infer a supported type for {name!r}")
