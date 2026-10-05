"""Ask an LLM for semantic tests that JSON Schema rules cannot derive."""

from __future__ import annotations

from pydantic import Field

from api_doctor.generation.workflow_discovery import build_catalog_context
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.models import RuleRecord, SemanticTestCandidate, StrictModel


class SemanticDiscovery(StrictModel):
    candidates: list[SemanticTestCandidate] = Field(default_factory=list, max_length=30)
    ambiguities: list[str] = Field(default_factory=list)


def build_semantic_test_prompt(
    catalog: OperationCatalog,
    rules: list[RuleRecord],
) -> dict[str, object]:
    return {
        "task": "Propose semantic API test candidates for natural-language constraints that cannot be derived from JSON Schema alone. Output proposals only; do not execute requests or generate code.",
        "supported_mutations": {
            "set_value": "Set one top-level JSON request field to a concrete value.",
            "omit_field": "Remove one top-level JSON request field.",
            "field_relation": "Set two top-level numeric or date-time fields to violate a stated lt/lte/eq/ne/gte/gt relation.",
        },
        "constraints": [
            "Use only operation_id and top-level request body fields present in the catalog.",
            "A candidate may combine multiple mutations to establish a condition and violate its consequence.",
            "Do not repeat constraints that are already directly represented by required, enum, format, minimum, maximum, minLength, maxLength, minItems, or maxItems.",
            "A compilable candidate needs evidence-backed expected status codes.",
            "For a user rule, copy its exact id and expected_status_codes; never invent either.",
            "For an OpenAPI description, use source.kind=openapi_description, copy its exact description_source id, and use only declared response status codes.",
            "If a rule is ambiguous or cannot be represented by the supported mutations, return not_compilable or explain it in ambiguities.",
        ],
        "catalog": build_catalog_context(catalog),
        "business_rules": [rule.model_dump(mode="json") for rule in rules],
        "output_json_schema": SemanticDiscovery.model_json_schema(),
    }


def discover_semantic_tests(
    catalog: OperationCatalog,
    rules: list[RuleRecord],
    gateway: ModelGateway,
) -> ModelResponse[SemanticDiscovery]:
    return gateway.complete(
        build_semantic_test_prompt(catalog, rules),
        response_model=SemanticDiscovery,
        trace_label="semantic-test-discovery",
    )
