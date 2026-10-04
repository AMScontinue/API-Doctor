"""Validate deterministic HTTP observations against OpenAPI responses."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from api_doctor.openapi.models import Operation, ResponseSpec


def _validation_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_validation_schema(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {key: _validation_schema(item) for key, item in value.items() if key != "nullable"}
    for bound in ("Minimum", "Maximum"):
        exclusive_key = f"exclusive{bound}"
        regular_key = bound[0].lower() + bound[1:]
        exclusive = result.get(exclusive_key)
        if isinstance(exclusive, bool):
            if exclusive and regular_key in result:
                result[exclusive_key] = result.pop(regular_key)
            else:
                result.pop(exclusive_key, None)
    if value.get("nullable") is True and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


def _response_for(operation: Operation, status: int) -> ResponseSpec | None:
    exact = str(status)
    response_range = f"{status // 100}XX"
    for key in (exact, response_range, response_range.lower(), "default"):
        match = next((item for item in operation.responses if item.status_code == key), None)
        if match:
            return match
    return None


def validate_response(
    operation: Operation,
    *,
    status: int,
    headers: dict[str, str],
    body: Any,
) -> list[str]:
    violations: list[str] = []
    response = _response_for(operation, status)
    if response is None:
        return [f"status {status} is not declared by the OpenAPI operation"]
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if response.media_type and content_type != response.media_type.lower():
        violations.append(
            f"Content-Type {content_type or '<missing>'!r} does not match {response.media_type!r}"
        )
    if response.json_schema is not None:
        validator = Draft202012Validator(
            _validation_schema(response.json_schema), format_checker=FormatChecker()
        )
        for error in validator.iter_errors(body):
            location = "$" + "".join(f"[{item!r}]" for item in error.absolute_path)
            violations.append(f"response schema violation at {location}: {error.message}")
    return violations
