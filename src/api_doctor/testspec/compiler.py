"""Deterministic compiler from validated semantic candidates to executable cases."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .models import (
    Assertion,
    Boundary,
    CompiledTestCase,
    FieldRelation,
    OmitField,
    Operation,
    SemanticTestCandidate,
    SetValue,
)


class CompileError(ValueError):
    """A candidate cannot safely be compiled by the supported M0 IR."""


def _field_name(path: str) -> str:
    if not path.startswith("$.") or len(path) < 3 or "." in path[2:] or "[" in path:
        raise CompileError(f"M0 only supports top-level JSON paths: {path}")
    return path[2:]


def _properties(operation: Operation) -> dict[str, dict[str, Any]]:
    return operation.request_schema.get("properties", {})


def _default_value(name: str, schema: dict[str, Any], anchor: datetime) -> Any:
    if "example" in schema:
        return deepcopy(schema["example"])
    if "default" in schema:
        return deepcopy(schema["default"])
    if schema.get("enum"):
        return deepcopy(schema["enum"][0])
    field_type = schema.get("type")
    field_format = schema.get("format")
    if field_type == "string" and field_format == "date-time":
        return anchor.isoformat().replace("+00:00", "Z")
    if field_type == "string" and field_format == "email":
        return f"{name}@example.test"
    if field_type == "string":
        value = f"test-{name}"
        minimum_length = int(schema.get("minLength", 0))
        return value.ljust(minimum_length, "x")
    if field_type == "integer":
        return int(schema.get("minimum", 1))
    if field_type == "number":
        return float(schema.get("minimum", 1.0))
    if field_type == "boolean":
        return True
    if field_type == "array":
        return []
    if field_type == "object":
        return {}
    raise CompileError(f"cannot generate baseline value for field {name}")


def _build_baseline(operation: Operation, anchor: datetime) -> dict[str, Any]:
    properties = _properties(operation)
    return {
        name: _default_value(name, properties[name], anchor)
        for name in operation.request_schema.get("required", [])
    }


def _ensure_field(operation: Operation, path: str) -> tuple[str, dict[str, Any]]:
    name = _field_name(path)
    schema = _properties(operation).get(name)
    if schema is None:
        raise CompileError(f"unknown field path {path} for {operation.operation_id}")
    return name, schema


def _delta_value(relation: FieldRelation) -> timedelta | int | float:
    if relation.delta.unit == "number":
        return relation.delta.value
    units = {
        "second": "seconds",
        "minute": "minutes",
        "hour": "hours",
        "day": "days",
    }
    return timedelta(**{units[relation.delta.unit]: relation.delta.value})


def _apply_relation(
    body: dict[str, Any], operation: Operation, relation: FieldRelation, anchor: datetime
) -> None:
    left_name, left_schema = _ensure_field(operation, relation.left)
    right_name, right_schema = _ensure_field(operation, relation.right)
    if left_schema.get("type") != right_schema.get("type"):
        raise CompileError("field_relation requires matching field types")
    delta = _delta_value(relation)
    if left_schema.get("format") == right_schema.get("format") == "date-time":
        right: Any = anchor
    elif left_schema.get("type") in {"integer", "number"}:
        right = _default_value(right_name, right_schema, anchor)
    else:
        raise CompileError("M0 field_relation supports numeric and date-time fields")
    if relation.operator == "lt":
        left = right - delta
    elif relation.operator == "lte":
        left = right
    elif relation.operator == "eq":
        left = right
    elif relation.operator == "ne":
        left = right + delta
    elif relation.operator == "gte":
        left = right
    else:
        left = right + delta
    if isinstance(left, datetime):
        body[left_name] = left.isoformat().replace("+00:00", "Z")
        body[right_name] = right.isoformat().replace("+00:00", "Z")
    else:
        body[left_name] = left
        body[right_name] = right


def compile_candidate(
    candidate: SemanticTestCandidate,
    operations: dict[str, Operation],
    *,
    case_number: int,
    seed: int,
    anchor: datetime,
) -> CompiledTestCase:
    if candidate.status != "compilable" or not candidate.operation_id:
        raise CompileError(candidate.unsupported_reason or "candidate is not compilable")
    operation = operations.get(candidate.operation_id)
    if operation is None:
        raise CompileError(f"unknown operation_id {candidate.operation_id}")
    body = _build_baseline(operation, anchor)
    intentionally_omitted: set[str] = set()
    for mutation in candidate.mutations:
        if isinstance(mutation, SetValue):
            field, _ = _ensure_field(operation, mutation.path)
            body[field] = mutation.value
        elif isinstance(mutation, OmitField):
            field, _ = _ensure_field(operation, mutation.path)
            body.pop(field, None)
            intentionally_omitted.add(field)
        elif isinstance(mutation, Boundary):
            field, schema = _ensure_field(operation, mutation.path)
            if mutation.position in {"below_minimum", "at_minimum"}:
                if "minimum" not in schema:
                    raise CompileError(f"field {field} has no minimum")
                body[field] = schema["minimum"] - (1 if mutation.position == "below_minimum" else 0)
            else:
                if "maximum" not in schema:
                    raise CompileError(f"field {field} has no maximum")
                body[field] = schema["maximum"] + (1 if mutation.position == "above_maximum" else 0)
        elif isinstance(mutation, FieldRelation):
            _apply_relation(body, operation, mutation, anchor)
    required = set(operation.request_schema.get("required", []))
    missing = required - set(body)
    if missing != (intentionally_omitted & required):
        raise CompileError(f"baseline is missing required fields: {sorted(missing)}")
    assert candidate.expected is not None
    return CompiledTestCase(
        case_id=f"TC-M0-{case_number:03d}",
        rule_id=candidate.rule_id,
        operation_id=operation.operation_id,
        method=operation.method,
        path=operation.path,
        purpose=candidate.purpose,
        body=body,
        assertions=[
            Assertion(
                allowed=candidate.expected.status_codes,
                source=candidate.source.kind,
                evidence_ref=candidate.source.id,
            )
        ],
        seed=seed,
        time_anchor=anchor,
    )
