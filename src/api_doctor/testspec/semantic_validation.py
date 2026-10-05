"""Deterministically validate and compile untrusted semantic test proposals."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from api_doctor.openapi.models import Operation, OperationCatalog
from api_doctor.openapi.schema_validation import schema_errors
from api_doctor.testspec.models import (
    CompiledTestCase,
    FieldRelation,
    OmitField,
    RuleRecord,
    SemanticTestCandidate,
    SetValue,
    StrictModel,
)
from api_doctor.testspec.openapi_compiler import OpenAPICompileError, compile_openapi_candidate


class CandidateReview(StrictModel):
    candidate: SemanticTestCandidate
    verdict: Literal["accepted", "rejected"]
    errors: list[str] = Field(default_factory=list)
    compiled_case: CompiledTestCase | None = None


class SemanticValidationResult(StrictModel):
    reviews: list[CandidateReview]

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.reviews),
            "accepted": sum(review.verdict == "accepted" for review in self.reviews),
            "rejected": sum(review.verdict == "rejected" for review in self.reviews),
            "compiled": sum(review.compiled_case is not None for review in self.reviews),
            "accepted_not_compilable": sum(
                review.verdict == "accepted" and review.candidate.status == "not_compilable"
                for review in self.reviews
            ),
        }


def _field(operation: Operation, path: str) -> tuple[str, dict[str, Any]] | None:
    if operation.request_body is None or not path.startswith("$."):
        return None
    properties = operation.request_body.json_schema.get("properties", {})
    schema = properties.get(path[2:]) if isinstance(properties, dict) else None
    return (path[2:], schema) if isinstance(schema, dict) else None


def _validate_source(
    candidate: SemanticTestCandidate,
    operation: Operation | None,
    rules: dict[str, RuleRecord],
) -> list[str]:
    errors: list[str] = []
    source = candidate.source
    if source.kind == "business_rule":
        rule = rules.get(source.id)
        if rule is None:
            return [f"unknown business rule {source.id!r}"]
        if candidate.rule_id != rule.id:
            errors.append("candidate rule_id does not match its business rule source")
        if rule.operation_id and candidate.operation_id != rule.operation_id:
            errors.append("candidate operation_id drifted from its business rule")
        if candidate.status == "compilable":
            if rule.expected_status_codes is None:
                errors.append("business rule does not provide expected status codes")
            elif candidate.expected is None or set(candidate.expected.status_codes) != set(
                rule.expected_status_codes
            ):
                errors.append("candidate expected statuses drifted from its business rule")
    elif source.kind == "openapi_description":
        if operation is None:
            return ["OpenAPI description candidate requires a known operation_id"]
        expected_source = f"{operation.source_pointer}/description"
        if source.id != expected_source:
            errors.append("candidate does not cite the selected operation description")
        if not operation.description:
            errors.append("selected operation has no description evidence")
        if candidate.status == "compilable" and candidate.expected is not None:
            declared = {
                int(response.status_code)
                for response in operation.responses
                if response.status_code.isdigit()
            }
            if not set(candidate.expected.status_codes).issubset(declared):
                errors.append("candidate expected statuses are not declared by the operation")
            if any(code < 400 or code >= 500 for code in candidate.expected.status_codes):
                errors.append("semantic violation expectation must use declared 4xx statuses")
    else:
        errors.append(f"semantic discovery cannot use source kind {source.kind!r}")
    return errors


def _validate_relation(
    operation: Operation,
    mutation: FieldRelation,
) -> list[str]:
    left = _field(operation, mutation.left)
    right = _field(operation, mutation.right)
    if left is None or right is None:
        return ["field_relation references an unknown top-level request field"]
    left_schema, right_schema = left[1], right[1]
    if left_schema.get("type") != right_schema.get("type"):
        return ["field_relation requires matching field types"]
    both_dates = left_schema.get("format") == right_schema.get("format") == "date-time"
    both_numbers = left_schema.get("type") in {"integer", "number"}
    if both_dates and mutation.delta.unit == "number":
        return ["date-time field_relation requires a time delta"]
    if both_numbers and mutation.delta.unit != "number":
        return ["numeric field_relation requires a numeric delta"]
    if not both_dates and not both_numbers:
        return ["field_relation supports only matching numeric or date-time fields"]
    return []


def _validate_mutations(
    candidate: SemanticTestCandidate,
    operation: Operation,
) -> list[str]:
    errors: list[str] = []
    required = (
        set(operation.request_body.json_schema.get("required", []))
        if operation.request_body
        else set()
    )
    for mutation in candidate.mutations:
        if isinstance(mutation, SetValue):
            field = _field(operation, mutation.path)
            if field is None:
                errors.append(f"unknown top-level request field {mutation.path}")
            else:
                errors.extend(
                    f"value for {mutation.path} violates its JSON Schema: {error.message}"
                    for error in schema_errors(field[1], mutation.value)
                )
        elif isinstance(mutation, OmitField):
            field = _field(operation, mutation.path)
            if field is None:
                errors.append(f"unknown top-level request field {mutation.path}")
            elif field[0] in required:
                errors.append(
                    f"omitting globally required field {mutation.path} duplicates a mechanical test"
                )
        elif isinstance(mutation, FieldRelation):
            errors.extend(_validate_relation(operation, mutation))
        else:
            errors.append(f"mutation {mutation.kind!r} is outside semantic discovery scope")
    return errors


def validate_semantic_candidates(
    candidates: list[SemanticTestCandidate],
    catalog: OperationCatalog,
    rules: list[RuleRecord],
    *,
    seed: int,
    anchor: datetime,
) -> SemanticValidationResult:
    """Review every proposal independently; one bad candidate never aborts the batch."""
    rule_index = {rule.id: rule for rule in rules}
    reviews: list[CandidateReview] = []
    for index, candidate in enumerate(candidates, start=1):
        operation = (
            catalog.operations.get(candidate.operation_id) if candidate.operation_id else None
        )
        errors: list[str] = []
        if operation is None and candidate.operation_id is not None:
            errors.append(f"unknown operation_id {candidate.operation_id!r}")
        errors.extend(_validate_source(candidate, operation, rule_index))
        if candidate.status == "compilable" and operation is not None:
            errors.extend(_validate_mutations(candidate, operation))

        compiled_case = None
        if candidate.status == "compilable" and not errors:
            try:
                compiled_case = compile_openapi_candidate(
                    candidate,
                    catalog,
                    case_number=index,
                    seed=seed,
                    anchor=anchor,
                )
            except OpenAPICompileError as error:
                errors.append(f"candidate could not be compiled: {error}")
        reviews.append(
            CandidateReview(
                candidate=candidate,
                verdict="rejected" if errors else "accepted",
                errors=errors,
                compiled_case=compiled_case,
            )
        )
    return SemanticValidationResult(reviews=reviews)
