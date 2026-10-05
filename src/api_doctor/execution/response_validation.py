"""Validate deterministic HTTP observations against OpenAPI responses."""

from __future__ import annotations

from typing import Any

from api_doctor.openapi.models import Operation, ResponseSpec
from api_doctor.openapi.schema_validation import schema_errors


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
        for error in schema_errors(response.json_schema, body):
            location = "$" + "".join(f"[{item!r}]" for item in error.absolute_path)
            violations.append(f"response schema violation at {location}: {error.message}")
    return violations
