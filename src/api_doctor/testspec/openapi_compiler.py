"""Compile validated TestSpec candidates against a normalized OpenAPI catalog."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from api_doctor.openapi.models import Operation, OperationCatalog, Parameter
from api_doctor.openapi.schema_validation import schema_errors
from api_doctor.testspec.models import (
    Assertion,
    Boundary,
    CompiledRequest,
    CompiledTestCase,
    FieldRelation,
    Mutation,
    OmitField,
    OmitParameter,
    ParameterBoundary,
    SemanticTestCandidate,
    SetValue,
    SetParameter,
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
    operation: Operation,
    *,
    seed: int,
    anchor: datetime,
    parameter_overrides: dict[tuple[str, str], Any] | None = None,
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
    for (location, name), value in (parameter_overrides or {}).items():
        if location not in locations:
            raise OpenAPICompileError(f"unsupported parameter override location {location!r}")
        parameter = next(
            (
                item
                for item in operation.parameters
                if item.location == location and item.name == name
            ),
            None,
        )
        if parameter is None:
            raise OpenAPICompileError(
                f"unknown {location} parameter {name!r} for {operation.operation_id}"
            )
        locations[location][name] = deepcopy(value)

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
    return [error.message for error in schema_errors(schema, value)]


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
    mutations: list[Mutation],
    anchor: datetime,
) -> Any:
    if not isinstance(body, dict):
        if all(isinstance(item, UseBaseline) for item in mutations):
            return body
        raise OpenAPICompileError("body mutations require an object request schema")
    mutated = deepcopy(body)
    for mutation in mutations:
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


def _parameter(operation: Operation, location: str, name: str) -> Parameter:
    parameter = next(
        (
            item
            for item in operation.parameters
            if item.location == location and item.name == name
        ),
        None,
    )
    if parameter is None:
        raise OpenAPICompileError(f"unknown {location} parameter {name!r}")
    return parameter


def _parameter_values(request: CompiledRequest, location: str) -> dict[str, Any]:
    return {
        "path": request.path_parameters,
        "query": request.query_parameters,
        "header": request.headers,
        "cookie": request.cookies,
    }[location]


def _validate_parameters(operation: Operation, request: CompiledRequest) -> list[str]:
    errors: list[str] = []
    for parameter in operation.parameters:
        values = _parameter_values(request, parameter.location)
        if parameter.required and parameter.name not in values:
            errors.append(f"required {parameter.location} parameter {parameter.name!r} is missing")
            continue
        if parameter.name in values:
            errors.extend(
                f"{parameter.location} parameter {parameter.name!r}: {message}"
                for message in _validate(parameter.json_schema, values[parameter.name])
            )
    return errors


def _apply_parameter_mutations(
    request: CompiledRequest,
    operation: Operation,
    candidate: SemanticTestCandidate,
) -> None:
    for mutation in candidate.mutations:
        if isinstance(mutation, SetParameter):
            _parameter(operation, mutation.location, mutation.name)
            _parameter_values(request, mutation.location)[mutation.name] = deepcopy(mutation.value)
        elif isinstance(mutation, OmitParameter):
            _parameter(operation, mutation.location, mutation.name)
            _parameter_values(request, mutation.location).pop(mutation.name, None)
        elif isinstance(mutation, ParameterBoundary):
            parameter = _parameter(operation, mutation.location, mutation.name)
            values = _parameter_values(request, mutation.location)
            if mutation.position == "below_minimum":
                if "minimum" not in parameter.json_schema:
                    raise OpenAPICompileError(f"parameter {mutation.name!r} has no minimum")
                values[mutation.name] = parameter.json_schema["minimum"] - 1
            else:
                if "maximum" not in parameter.json_schema:
                    raise OpenAPICompileError(f"parameter {mutation.name!r} has no maximum")
                values[mutation.name] = parameter.json_schema["maximum"] + 1


BODY_MUTATIONS = (UseBaseline, SetValue, OmitField, Boundary, FieldRelation)


def compile_openapi_candidate(
    candidate: SemanticTestCandidate,
    catalog: OperationCatalog,
    *,
    case_number: int,
    seed: int,
    anchor: datetime,
    parameter_overrides: dict[tuple[str, str], Any] | None = None,
    body_overrides: dict[str, Any] | None = None,
) -> CompiledTestCase:
    """Materialize one candidate and verify its baseline/mutation invariants."""
    if candidate.status != "compilable" or not candidate.operation_id:
        raise OpenAPICompileError(candidate.unsupported_reason or "candidate is not compilable")
    operation = catalog.operations.get(candidate.operation_id)
    if operation is None:
        raise OpenAPICompileError(f"unknown operation_id {candidate.operation_id!r}")
    if candidate.expected is None:
        raise OpenAPICompileError("candidate has no evidence-backed expectation")

    request = _request_baseline(
        operation,
        seed=seed,
        anchor=anchor,
        parameter_overrides=parameter_overrides,
    )
    if body_overrides:
        schema = _body_schema(operation)
        if schema is None or not isinstance(request.body, dict):
            raise OpenAPICompileError(
                f"operation {operation.operation_id!r} has no object request body"
            )
        for name, value in body_overrides.items():
            _field(f"$.{name}", schema)
            request.body[name] = deepcopy(value)
    baseline_parameter_errors = _validate_parameters(operation, request)
    if baseline_parameter_errors:
        raise OpenAPICompileError(
            f"generated parameter baseline is not schema-valid: {baseline_parameter_errors[0]}"
        )
    _apply_parameter_mutations(request, operation, candidate)
    parameter_errors = _validate_parameters(operation, request)
    schema = _body_schema(operation)
    if schema is not None:
        baseline_errors = _validate(schema, request.body)
        if baseline_errors:
            raise OpenAPICompileError(f"generated baseline is not schema-valid: {baseline_errors[0]}")
        body_mutations = [
            mutation for mutation in candidate.mutations if isinstance(mutation, BODY_MUTATIONS)
        ]
        request.body = _apply_mutations(request.body, schema, body_mutations, anchor)
        mutation_errors = _validate(schema, request.body)
        if candidate.is_baseline and mutation_errors:
            raise OpenAPICompileError(f"happy-path request became invalid: {mutation_errors[0]}")
        if (
            candidate.source.kind == "openapi"
            and not candidate.is_baseline
            and not mutation_errors
            and not parameter_errors
        ):
            raise OpenAPICompileError("OpenAPI negative mutation did not violate its request schema")
    elif any(
        isinstance(item, (SetValue, OmitField, Boundary, FieldRelation))
        for item in candidate.mutations
    ):
        raise OpenAPICompileError("candidate mutates a request body that the operation does not define")
    elif (
        candidate.source.kind == "openapi"
        and not candidate.is_baseline
        and not parameter_errors
    ):
        raise OpenAPICompileError("OpenAPI negative mutation did not violate its parameter schema")

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
