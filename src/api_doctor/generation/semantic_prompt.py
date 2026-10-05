"""Prompt construction and safe log views for semantic candidate extraction."""

from __future__ import annotations

import json

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


def build_repair_prompt(
    prompt: dict[str, object], invalid_output: str, validation_error: str
) -> dict[str, object]:
    return {
        **prompt,
        "task": "Repair the previous output so it satisfies the output JSON schema and remains faithful to the rule. If the rule has no expected status codes, it cannot be compilable because a compilable candidate requires an evidence-backed expected result. Output JSON only.",
        "previous_invalid_output": invalid_output,
        "validation_error": validation_error,
    }


def build_prompt_log_view(prompt: dict[str, object]) -> dict[str, object]:
    rule = prompt["rule"]
    operations = prompt["operations"]
    view: dict[str, object] = {
        "task": prompt["task"],
        "composition_rule": prompt["composition_rule"],
        "type_rule": prompt["type_rule"],
        "rule": rule,
        "operations": [
            {
                "operation_id": operation.get("operation_id"),
                "method": operation.get("method"),
                "path": operation.get("path"),
                "request_schema": operation.get("request_schema"),
            }
            for operation in operations
            if isinstance(operation, dict)
            and (
                not isinstance(rule, dict)
                or not rule.get("operation_id")
                or operation.get("operation_id") == rule.get("operation_id")
            )
        ],
    }
    if "previous_invalid_output" in prompt:
        try:
            view["previous_invalid_output"] = json.loads(
                str(prompt["previous_invalid_output"])
            )
        except json.JSONDecodeError:
            view["previous_invalid_output"] = "<invalid JSON omitted>"
        view["validation_error"] = prompt.get("validation_error")
    return view
