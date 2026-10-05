"""Shared JSON Schema validation for normalized OpenAPI schemas."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


def _draft_2020_schema(value: Any) -> Any:
    """Translate the OpenAPI 3.0 nullable and exclusive-bound forms used by the MVP."""
    if isinstance(value, list):
        return [_draft_2020_schema(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {
        key: _draft_2020_schema(item)
        for key, item in value.items()
        if key != "nullable"
    }
    for bound in ("Minimum", "Maximum"):
        exclusive = f"exclusive{bound}"
        regular = bound[0].lower() + bound[1:]
        if isinstance(result.get(exclusive), bool):
            if result[exclusive] and regular in result:
                result[exclusive] = result.pop(regular)
            else:
                result.pop(exclusive, None)
    if value.get("nullable") is True and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


def schema_errors(schema: dict[str, Any], value: Any) -> list[ValidationError]:
    validator = Draft202012Validator(
        _draft_2020_schema(schema),
        format_checker=FormatChecker(),
    )
    return list(validator.iter_errors(value))
