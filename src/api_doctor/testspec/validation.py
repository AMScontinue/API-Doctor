"""Context validation for untrusted semantic candidates."""

from __future__ import annotations

from api_doctor.evaluation.models import BenchmarkOperation
from api_doctor.testspec.models import FieldRelation, RuleRecord, SemanticTestCandidate


class CandidateContextError(ValueError):
    """The candidate is structurally valid but not faithful to its source context."""


def validate_candidate_context(
    candidate: SemanticTestCandidate,
    rule: RuleRecord,
    operations: dict[str, BenchmarkOperation],
) -> None:
    if candidate.rule_id != rule.id:
        raise CandidateContextError("candidate rule_id does not match the input rule")
    if candidate.source.kind != "business_rule" or candidate.source.id != rule.id:
        raise CandidateContextError("candidate source does not match the input business rule")
    if (
        rule.operation_id
        and candidate.status == "compilable"
        and candidate.operation_id != rule.operation_id
    ):
        raise CandidateContextError("candidate operation_id drifted from the input rule")
    if candidate.operation_id and candidate.operation_id not in operations:
        raise CandidateContextError(f"unknown operation_id {candidate.operation_id}")

    if candidate.expected is not None:
        if rule.expected_status_codes is None:
            raise CandidateContextError("candidate invented expected status codes")
        if set(candidate.expected.status_codes) != set(rule.expected_status_codes):
            raise CandidateContextError("candidate expected status codes drifted from the input rule")
    elif candidate.status == "compilable":
        raise CandidateContextError("compilable candidate has no expected status source")

    if candidate.status != "compilable" or not candidate.operation_id:
        return
    properties = operations[candidate.operation_id].request_schema.get("properties", {})
    for mutation in candidate.mutations:
        paths = []
        if hasattr(mutation, "path"):
            paths.append(mutation.path)
        if hasattr(mutation, "left"):
            paths.extend((mutation.left, mutation.right))
        for path in paths:
            field = path[2:]
            if field not in properties:
                raise CandidateContextError(
                    f"unknown field path {path} for {candidate.operation_id}"
                )
        if isinstance(mutation, FieldRelation):
            left = properties[mutation.left[2:]]
            right = properties[mutation.right[2:]]
            if left.get("type") != right.get("type"):
                raise CandidateContextError("field_relation requires matching field types")
            both_dates = left.get("format") == right.get("format") == "date-time"
            both_numbers = left.get("type") in {"integer", "number"}
            if both_dates and mutation.delta.unit == "number":
                raise CandidateContextError("date-time field_relation requires a time delta unit")
            if both_numbers and mutation.delta.unit != "number":
                raise CandidateContextError("numeric field_relation requires the number delta unit")
            if not both_dates and not both_numbers:
                raise CandidateContextError(
                    "field_relation only supports numeric or date-time fields"
                )
