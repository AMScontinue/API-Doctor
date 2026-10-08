"""Deterministically derive TestSpec candidates from OpenAPI request schemas."""

from __future__ import annotations

from typing import Any, Iterable

from api_doctor.openapi.models import Operation, OperationCatalog
from api_doctor.testspec.models import (
    Boundary,
    Expected,
    Mutation,
    OmitField,
    OmitParameter,
    ParameterBoundary,
    SemanticTestCandidate,
    SetValue,
    SetParameter,
    Source,
    UseBaseline,
)


SUPPORTED_FORMATS = {"email", "date", "date-time", "uuid", "uri", "ipv4", "ipv6"}
VALIDATION_STATUS_CODES = {400, 409, 415, 422}


def _declared_codes(operation: Operation, category: int) -> list[int]:
    result: list[int] = []
    for response in operation.responses:
        if response.status_code.isdigit():
            code = int(response.status_code)
            if code // 100 == category:
                result.append(code)
    return sorted(set(result))


def _negative_codes(operation: Operation) -> list[int]:
    return [code for code in _declared_codes(operation, 4) if code in VALIDATION_STATUS_CODES]


def _candidate(
    operation: Operation,
    *,
    suffix: str,
    purpose: str,
    mutation: UseBaseline | SetValue | OmitField | Boundary | SetParameter | OmitParameter | ParameterBoundary,
    status_codes: list[int],
    evidence_ref: str,
    source_kind: str = "openapi",
) -> SemanticTestCandidate:
    rule_id = f"schema:{operation.operation_id}:{suffix}"
    if not status_codes:
        return SemanticTestCandidate(
            rule_id=rule_id,
            status="not_compilable",
            operation_id=operation.operation_id,
            purpose=purpose,
            source=Source(kind=source_kind, id=evidence_ref),
            unsupported_reason="OpenAPI does not declare an evidence-backed response status for this scenario",
        )
    return SemanticTestCandidate(
        rule_id=rule_id,
        status="compilable",
        operation_id=operation.operation_id,
        purpose=purpose,
        mutations=[mutation],
        expected=Expected(status_codes=status_codes),
        source=Source(kind=source_kind, id=evidence_ref),
    )


def _tests_path_parameter(
    mutation: Mutation,
) -> bool:
    return isinstance(mutation, (SetParameter, OmitParameter, ParameterBoundary)) and (
        mutation.location == "path"
    )


def _required_path_parameters(operation: Operation) -> set[str]:
    return {
        parameter.name
        for parameter in operation.parameters
        if parameter.location == "path" and parameter.required
    }


def _linked_path_parameters(catalog: OperationCatalog, operation: Operation) -> set[str]:
    linked: set[str] = set()
    for producer in catalog.operations.values():
        for response in producer.responses:
            for link in response.links.values():
                if link.operation_id != operation.operation_id:
                    continue
                for name in link.parameters:
                    matches = [item for item in operation.parameters if item.name == name]
                    if len(matches) == 1 and matches[0].location == "path":
                        linked.add(name)
    return linked


def _apply_setup_policy(
    candidates: list[SemanticTestCandidate],
    operation: Operation,
    *,
    linked_setup_available: bool,
) -> list[SemanticTestCandidate]:
    if not _required_path_parameters(operation) or linked_setup_available:
        return candidates
    reason = (
        "A standalone request cannot prove that its generated path parameter "
        "identifies an existing resource; use a setup workflow and bind the "
        "producer response instead"
    )
    return [
        candidate
        if candidate.source.kind == "heuristic"
        or any(_tests_path_parameter(item) for item in candidate.mutations)
        else candidate.model_copy(
            update={
                "status": "not_compilable",
                "mutations": [],
                "expected": None,
                "unsupported_reason": reason,
            }
        )
        for candidate in candidates
    ]


def _invalid_enum_value(values: list[Any]) -> Any | None:
    if not values:
        return None
    sample = values[0]
    if isinstance(sample, bool):
        boolean_values = {item for item in values if isinstance(item, bool)}
        return None if {True, False}.issubset(boolean_values) else not sample
    if isinstance(sample, str):
        value = "__api_doctor_invalid_enum__"
        while value in values:
            value += "_"
        return value
    if isinstance(sample, int):
        value = max((item for item in values if isinstance(item, int)), default=0) + 1
        while value in values:
            value += 1
        return value
    if isinstance(sample, float):
        value = max((item for item in values if isinstance(item, (int, float))), default=0) + 0.5
        while value in values:
            value += 1
        return value
    return None


def _field_candidates(
    operation: Operation,
    field: str,
    schema: dict[str, Any],
    *,
    required: bool,
) -> Iterable[SemanticTestCandidate]:
    path = f"$.{field}"
    schema_pointer = f"{operation.source_pointer}/requestBody/content/{operation.request_body.media_type}/schema"
    pointer = f"{schema_pointer}/properties/{field}"
    negative = _negative_codes(operation)

    if required:
        yield _candidate(
            operation,
            suffix=f"missing-{field}",
            purpose=f"Verify required request field {field!r} is rejected when omitted",
            mutation=OmitField(kind="omit_field", path=path),
            status_codes=negative,
            evidence_ref=f"{schema_pointer}/required",
        )

    if "minimum" in schema:
        yield _candidate(
            operation,
            suffix=f"{field}-below-minimum",
            purpose=f"Verify {field!r} below minimum is rejected",
            mutation=Boundary(kind="boundary", path=path, position="below_minimum"),
            status_codes=negative,
            evidence_ref=f"{pointer}/minimum",
        )
    if "maximum" in schema:
        yield _candidate(
            operation,
            suffix=f"{field}-above-maximum",
            purpose=f"Verify {field!r} above maximum is rejected",
            mutation=Boundary(kind="boundary", path=path, position="above_maximum"),
            status_codes=negative,
            evidence_ref=f"{pointer}/maximum",
        )
    exclusive_minimum = schema.get("exclusiveMinimum")
    exclusive_minimum_value = (
        schema.get("minimum")
        if exclusive_minimum is True
        else exclusive_minimum
        if isinstance(exclusive_minimum, (int, float)) and not isinstance(exclusive_minimum, bool)
        else None
    )
    if isinstance(exclusive_minimum_value, (int, float)):
        yield _candidate(
            operation,
            suffix=f"{field}-at-exclusive-minimum",
            purpose=f"Verify {field!r} equal to exclusive minimum is rejected",
            mutation=SetValue(kind="set_value", path=path, value=exclusive_minimum_value),
            status_codes=negative,
            evidence_ref=f"{pointer}/exclusiveMinimum",
        )
    exclusive_maximum = schema.get("exclusiveMaximum")
    exclusive_maximum_value = (
        schema.get("maximum")
        if exclusive_maximum is True
        else exclusive_maximum
        if isinstance(exclusive_maximum, (int, float)) and not isinstance(exclusive_maximum, bool)
        else None
    )
    if isinstance(exclusive_maximum_value, (int, float)):
        yield _candidate(
            operation,
            suffix=f"{field}-at-exclusive-maximum",
            purpose=f"Verify {field!r} equal to exclusive maximum is rejected",
            mutation=SetValue(kind="set_value", path=path, value=exclusive_maximum_value),
            status_codes=negative,
            evidence_ref=f"{pointer}/exclusiveMaximum",
        )

    enum = schema.get("enum")
    if isinstance(enum, list):
        invalid = _invalid_enum_value(enum)
        if invalid is not None:
            yield _candidate(
                operation,
                suffix=f"{field}-invalid-enum",
                purpose=f"Verify an undeclared enum value for {field!r} is rejected",
                mutation=SetValue(kind="set_value", path=path, value=invalid),
                status_codes=negative,
                evidence_ref=f"{pointer}/enum",
            )

    field_format = schema.get("format")
    if schema.get("type") == "string" and field_format in SUPPORTED_FORMATS:
        yield _candidate(
            operation,
            suffix=f"{field}-invalid-format",
            purpose=f"Verify invalid {field_format!r} format for {field!r} is rejected",
            mutation=SetValue(kind="set_value", path=path, value=f"not-a-valid-{field_format}"),
            status_codes=negative,
            evidence_ref=f"{pointer}/format",
        )

    minimum_length = schema.get("minLength")
    if isinstance(minimum_length, int) and minimum_length > 0:
        yield _candidate(
            operation,
            suffix=f"{field}-below-min-length",
            purpose=f"Verify {field!r} shorter than minLength is rejected",
            mutation=SetValue(kind="set_value", path=path, value="x" * (minimum_length - 1)),
            status_codes=negative,
            evidence_ref=f"{pointer}/minLength",
        )
    maximum_length = schema.get("maxLength")
    if isinstance(maximum_length, int):
        yield _candidate(
            operation,
            suffix=f"{field}-above-max-length",
            purpose=f"Verify {field!r} longer than maxLength is rejected",
            mutation=SetValue(kind="set_value", path=path, value="x" * (maximum_length + 1)),
            status_codes=negative,
            evidence_ref=f"{pointer}/maxLength",
        )

    if schema.get("type") == "array":
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and minimum_items > 0:
            yield _candidate(
                operation,
                suffix=f"{field}-below-min-items",
                purpose=f"Verify {field!r} with fewer than minItems is rejected",
                mutation=SetValue(kind="set_value", path=path, value=[None] * (minimum_items - 1)),
                status_codes=negative,
                evidence_ref=f"{pointer}/minItems",
            )
        maximum_items = schema.get("maxItems")
        if isinstance(maximum_items, int):
            yield _candidate(
                operation,
                suffix=f"{field}-above-max-items",
                purpose=f"Verify {field!r} with more than maxItems is rejected",
                mutation=SetValue(kind="set_value", path=path, value=[None] * (maximum_items + 1)),
                status_codes=negative,
                evidence_ref=f"{pointer}/maxItems",
            )
        if schema.get("uniqueItems") is True:
            yield _candidate(
                operation,
                suffix=f"{field}-duplicate-items",
                purpose=f"Verify duplicate items in unique array {field!r} are rejected",
                mutation=SetValue(kind="set_value", path=path, value=["duplicate", "duplicate"]),
                status_codes=negative,
                evidence_ref=f"{pointer}/uniqueItems",
            )


def _parameter_candidates(operation: Operation) -> Iterable[SemanticTestCandidate]:
    negative = _negative_codes(operation)
    declared = {response.status_code for response in operation.responses}
    for parameter in operation.parameters:
        pointer = f"{operation.source_pointer}/parameters/{parameter.location}/{parameter.name}"
        suffix = f"{parameter.location}-{parameter.name}"
        if parameter.required and parameter.location != "path":
            yield _candidate(
                operation,
                suffix=f"missing-{suffix}",
                purpose=f"Verify required {parameter.location} parameter {parameter.name!r} is rejected",
                mutation=OmitParameter(
                    kind="omit_parameter", location=parameter.location, name=parameter.name
                ),
                status_codes=negative,
                evidence_ref=f"{pointer}/required",
            )
        if "minimum" in parameter.json_schema:
            yield _candidate(
                operation,
                suffix=f"{suffix}-below-minimum",
                purpose=f"Verify {parameter.location} parameter {parameter.name!r} below minimum is rejected",
                mutation=ParameterBoundary(
                    kind="parameter_boundary",
                    location=parameter.location,
                    name=parameter.name,
                    position="below_minimum",
                ),
                status_codes=negative,
                evidence_ref=f"{pointer}/schema/minimum",
            )
        if "maximum" in parameter.json_schema:
            yield _candidate(
                operation,
                suffix=f"{suffix}-above-maximum",
                purpose=f"Verify {parameter.location} parameter {parameter.name!r} above maximum is rejected",
                mutation=ParameterBoundary(
                    kind="parameter_boundary",
                    location=parameter.location,
                    name=parameter.name,
                    position="above_maximum",
                ),
                status_codes=negative,
                evidence_ref=f"{pointer}/schema/maximum",
            )
        if (
            operation.method == "GET"
            and parameter.location == "path"
            and "404" in declared
        ):
            missing_value: Any = (
                1_000_000_000
                if parameter.json_schema.get("type") in {"integer", "number"}
                else "__api_doctor_missing__"
            )
            yield _candidate(
                operation,
                suffix=f"{suffix}-not-found",
                purpose=f"Verify an unknown resource value for {parameter.name!r} returns 404",
                mutation=SetParameter(
                    kind="set_parameter",
                    location="path",
                    name=parameter.name,
                    value=missing_value,
                ),
                status_codes=[404],
                evidence_ref=f"{operation.source_pointer}/responses/404",
                source_kind="heuristic",
            )


def generate_schema_candidates(
    catalog: OperationCatalog,
    *,
    allow_linked_setup: bool = False,
) -> list[SemanticTestCandidate]:
    """Generate stable candidates for supported top-level JSON request constraints."""
    candidates: list[SemanticTestCandidate] = []
    for operation in catalog.operations.values():
        operation_candidates: list[SemanticTestCandidate] = []
        success = _declared_codes(operation, 2)
        operation_candidates.append(
            _candidate(
                operation,
                suffix="happy-path",
                purpose="Verify a schema-valid request is accepted",
                mutation=UseBaseline(kind="use_baseline"),
                status_codes=success,
                evidence_ref=f"{operation.source_pointer}/responses",
            )
        )
        operation_candidates.extend(_parameter_candidates(operation))
        request_body = operation.request_body
        if request_body is not None and request_body.media_type == "application/json":
            schema = request_body.json_schema
            if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
                required = set(schema.get("required", []))
                for field, field_schema in schema["properties"].items():
                    if isinstance(field_schema, dict):
                        operation_candidates.extend(
                            _field_candidates(
                                operation,
                                field,
                                field_schema,
                                required=field in required,
                            )
                        )
        linked_setup_available = allow_linked_setup and (
            _required_path_parameters(operation)
            <= _linked_path_parameters(catalog, operation)
        )
        candidates.extend(
            _apply_setup_policy(
                operation_candidates,
                operation,
                linked_setup_available=linked_setup_available,
            )
        )
    return candidates
