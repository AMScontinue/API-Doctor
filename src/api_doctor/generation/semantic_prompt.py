"""Prompt construction and safe log views for semantic candidate extraction."""

from __future__ import annotations

from api_doctor.evaluation.models import BenchmarkOperation
from api_doctor.testspec.models import RuleRecord, SemanticTestCandidate


def build_semantic_prompt(
    rule: RuleRecord, operations: dict[str, BenchmarkOperation]
) -> dict[str, object]:
    return {
        "task": "Convert one business rule into the supported TestSpec candidate. Return not_compilable only when no valid combination of supported mutations can represent it. Never invent operation IDs or fields.",
        "supported_mutations": ["set_value", "omit_field", "boundary", "field_relation"],
        "composition_rule": "A candidate may contain multiple mutations. Before returning not_compilable, consider whether several atomic mutations can jointly establish the business precondition and construct its violation. Do not assume one rule must map to one mutation.",
        "type_rule": "For field_relation, numeric fields require delta.unit=number; date-time fields require a time unit such as second, minute, hour, or day.",
        "path_format": "Every field path MUST be a top-level JSONPath in the exact form $.field_name. Bare names such as email or end_time are invalid.",
        "examples": {
            "valid_path": "$.email",
            "valid_relation": {
                "kind": "field_relation",
                "left": "$.end_time",
                "operator": "lt",
                "right": "$.start_time",
                "delta": {"value": 1, "unit": "hour"},
            },
        },
        "rule": rule.model_dump(mode="json"),
        "operations": [item.model_dump(mode="json") for item in operations.values()],
        "output_json_schema": SemanticTestCandidate.model_json_schema(),
    }
