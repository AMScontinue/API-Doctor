"""Compile validated TestSpec candidates against a normalized OpenAPI catalog."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from api_doctor.openapi.models import Operation, OperationCatalog
from api_doctor.testspec.models import (
    Assertion,
    Boundary,
    CompiledRequest,
    CompiledTestCase,
    FieldRelation,
    OmitField,
    SemanticTestCandidate,
    SetValue,
    UseBaseline,
)
from api_doctor.testspec.value_generator import ValueGenerationError, generate_value


class OpenAPICompileError(ValueError):
    """A candidate cannot become a validated request for its OpenAPI operation."""


def _body_schema(operation: Operation) -> dict[str, Any] | None:
    return operation.request_body.json_schema if operation.request_body else None


def _field(path: str, schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not path.startswith("$.") or "." in path[2:] or "[" in path:
        raise OpenAPICompileError(f"only top-level body paths are currently supported: {path}")
    name = path[2:]
    properties = schema.get("properties", {})
    field_schema = properties.get(name) if isinstance(properties, dict) else None
    if not isinstance(field_schema, dict):
        raise OpenAPICompileError(f"unknown request body path {path}")
    return name, field_schema


def _request_baseline(
    operation: Operation, *, seed: int, anchor: datetime
) -> CompiledRequest:
    locations: dict[str, dict[str, Any]] = {
        "path": {},
        "query": {},
        "header": {},
        "cookie": {},
    }
    for index, parameter in enumerate(operation.parameters):
        if parameter.required:
            try:
                locations[parameter.location][parameter.name] = generate_value(
                    parameter.json_schema,
                    name=parameter.name,
                    seed=seed + index + 1,
                    anchor=anchor,
                )
            except ValueGenerationError as exc:
                raise OpenAPICompileError(f"cannot generate parameter {parameter.name!r}: {exc}") from exc

    body = None
    if operation.request_body:
        try:
            body = generate_value(
                operation.request_body.json_schema,
                name="request-body",
                seed=seed,
                anchor=anchor,
            )
        except ValueGenerationError as exc:
            raise OpenAPICompileError(f"cannot generate request body: {exc}") from exc
    return CompiledRequest(
        method=operation.method,
        path_template=operation.path,
        path_parameters=locations["path"],
        query_parameters=locations["query"],
        headers=locations["header"],
        cookies=locations["cookie"],
        body=body,
        media_type=operation.request_body.media_type if operation.request_body else None,
    )


def _validate(schema: dict[str, Any], value: Any) -> list[str]:
    validator = Draft202012Validator(_validation_schema(schema), format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(value)]


def _validation_schema(value: Any) -> Any:
    """Translate the small OpenAPI 3.0 differences used by the MVP validator."""
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
    if value.get("nullable") is True:
        original_type = result.get("type")
        if isinstance(original_type, str):
            result["type"] = [original_type, "null"]
    return result


def _relation_delta(relation: FieldRelation) -> timedelta | int | float:
    if relation.delta.unit == "number":
        return relation.delta.value
    return timedelta(**{f"{relation.delta.unit}s": relation.delta.value})


def _apply_relation(
    body: dict[str, Any], schema: dict[str, Any], relation: FieldRelation, anchor: datetime
) -> None:
    left_name, left_schema = _field(relation.left, schema)
    right_name, right_schema = _field(relation.right, schema)
    delta = _relation_delta(relation)
    if left_schema.get("format") == right_schema.get("format") == "date-time":
        if not isinstance(delta, timedelta):
            raise OpenAPICompileError("date-time relation requires a time delta")
        right: Any = anchor
    elif left_schema.get("type") in {"integer", "number"} and left_schema.get("type") == right_schema.get("type"):
        if isinstance(delta, timedelta):
            raise OpenAPICompileError("numeric relation requires a numeric delta")
        right = generate_value(right_schema, name=right_name, seed=1, anchor=anchor)
    else:
        raise OpenAPICompileError("field relation requires matching numeric or date-time fields")
    left = {
        "lt": right - delta,
        "lte": right,
        "eq": right,
        "ne": right + delta,
        "gte": right,
        "gt": right + delta,
    }[relation.operator]
    if isinstance(left, datetime):
        body[left_name] = left.isoformat().replace("+00:00", "Z")
        body[right_name] = right.isoformat().replace("+00:00", "Z")
    else:
        body[left_name] = left
        body[right_name] = right


def _apply_mutations(
    body: Any,
    schema: dict[str, Any],
    candidate: SemanticTestCandidate,
    anchor: datetime,
) -> Any:
    if not isinstance(body, dict):
        if all(isinstance(item, UseBaseline) for item in candidate.mutations):
            return body
        raise OpenAPICompileError("body mutations require an object request schema")
    mutated = deepcopy(body)
    for mutation in candidate.mutations:
        if isinstance(mutation, UseBaseline):
            continue
        if isinstance(mutation, SetValue):
            name, _ = _field(mutation.path, schema)
            mutated[name] = deepcopy(mutation.value)
        elif isinstance(mutation, OmitField):
            name, _ = _field(mutation.path, schema)
            mutated.pop(name, None)
        elif isinstance(mutation, Boundary):
            name, field_schema = _field(mutation.path, schema)
            if mutation.position in {"below_minimum", "at_minimum"}:
                if "minimum" not in field_schema:
                    raise OpenAPICompileError(f"field {name!r} has no minimum")
                mutated[name] = field_schema["minimum"] - (1 if mutation.position == "below_minimum" else 0)
            else:
                if "maximum" not in field_schema:
                    raise OpenAPICompileError(f"field {name!r} has no maximum")
                mutated[name] = field_schema["maximum"] + (1 if mutation.position == "above_maximum" else 0)
        elif isinstance(mutation, FieldRelation):
            _apply_relation(mutated, schema, mutation, anchor)
    return mutated


def compile_openapi_candidate(
    candidate: SemanticTestCandidate,
    catalog: OperationCatalog,
    *,
    case_number: int,
    seed: int,
    anchor: datetime,
) -> CompiledTestCase:
    """Materialize one candidate and verify its baseline/mutation invariants."""
    if candidate.status != "compilable" or not candidate.operation_id:
        raise OpenAPICompileError(candidate.unsupported_reason or "candidate is not compilable")
    operation = catalog.operations.get(candidate.operation_id)
    if operation is None:
        raise OpenAPICompileError(f"unknown operation_id {candidate.operation_id!r}")
    if candidate.expected is None:
        raise OpenAPICompileError("candidate has no evidence-backed expectation")

    request = _request_baseline(operation, seed=seed, anchor=anchor)
    schema = _body_schema(operation)
    if schema is not None:
        baseline_errors = _validate(schema, request.body)
        if baseline_errors:
            raise OpenAPICompileError(f"generated baseline is not schema-valid: {baseline_errors[0]}")
        request.body = _apply_mutations(request.body, schema, candidate, anchor)
        is_happy_path = all(isinstance(item, UseBaseline) for item in candidate.mutations)
        mutation_errors = _validate(schema, request.body)
        if is_happy_path and mutation_errors:
            raise OpenAPICompileError(f"happy-path request became invalid: {mutation_errors[0]}")
        if candidate.source.kind == "openapi" and not is_happy_path and not mutation_errors:
            raise OpenAPICompileError("OpenAPI negative mutation did not violate its request schema")
    elif any(not isinstance(item, UseBaseline) for item in candidate.mutations):
        raise OpenAPICompileError("candidate mutates a request body that the operation does not define")

    return CompiledTestCase(
        case_id=f"TC-OAS-{case_number:03d}",
        rule_id=candidate.rule_id,
        operation_id=operation.operation_id,
        purpose=candidate.purpose,
        request=request,
        assertions=[
            Assertion(
                allowed=candidate.expected.status_codes,
                source=candidate.source.kind,
                evidence_ref=candidate.source.id,
            )
        ],
        risk="requires_approval" if operation.method == "DELETE" else "safe",
        seed=seed,
        time_anchor=anchor,
    )
