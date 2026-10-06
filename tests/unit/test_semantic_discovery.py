from pathlib import Path

import pytest

from api_doctor.generation.rules import load_business_rules
from api_doctor.generation.semantic_discovery import (
    SemanticDiscovery,
    build_semantic_test_prompt,
    discover_semantic_tests,
)
from api_doctor.llm.gateway import ModelCallTelemetry, ModelResponse
from api_doctor.openapi import load_openapi


ROOT = Path(__file__).resolve().parents[2]


class FakeGateway:
    def __init__(self, discovery: SemanticDiscovery) -> None:
        self.discovery = discovery
        self.prompt = None

    def complete(self, prompt, *, response_model, trace_label):
        self.prompt = prompt
        assert response_model is SemanticDiscovery
        assert trace_label == "semantic-test-discovery"
        return ModelResponse(
            value=self.discovery,
            telemetry=ModelCallTelemetry(calls=1, duration_ms=5),
        )


def _discovery() -> SemanticDiscovery:
    return SemanticDiscovery.model_validate(
        {
            "candidates": [
                {
                    "rule_id": "order-time-range",
                    "status": "compilable",
                    "operation_id": "createOrder",
                    "purpose": "Reject an end time before the start time",
                    "mutations": [
                        {
                            "kind": "field_relation",
                            "left": "$.end_time",
                            "operator": "lt",
                            "right": "$.start_time",
                            "delta": {"value": 1, "unit": "hour"},
                        }
                    ],
                    "expected": {"status_codes": [422]},
                    "source": {"kind": "business_rule", "id": "order-time-range"},
                }
            ],
            "ambiguities": [],
        }
    )


def test_semantic_prompt_contains_descriptions_rules_and_supported_dsl():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    rules = load_business_rules(ROOT / "examples/buggy-shop/rules.yaml")

    prompt = build_semantic_test_prompt(catalog, rules)

    create_order = next(
        item for item in prompt["catalog"]["operations"] if item["operation_id"] == "createOrder"
    )
    assert "End time must be later" in create_order["description"]
    assert create_order["description_source"].endswith("/description")
    assert {rule["id"] for rule in prompt["business_rules"]} == {
        "order-time-range",
        "discount-not-over-total",
        "cash-card-token-forbidden",
        "card-token-required",
        "unique-user-email",
        "cancelled-order-terminal",
        "repeated-order-status-conflict",
    }
    assert set(prompt["supported_mutations"]) == {
        "set_value",
        "omit_field",
        "field_relation",
    }


def test_semantic_discovery_returns_typed_candidates():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    rules = load_business_rules(ROOT / "examples/buggy-shop/rules.yaml")
    gateway = FakeGateway(_discovery())

    response = discover_semantic_tests(catalog, rules, gateway)

    candidate = response.value.candidates[0]
    assert candidate.operation_id == "createOrder"
    assert candidate.mutations[0].kind == "field_relation"
    assert gateway.prompt["business_rules"][0]["id"] == "order-time-range"


def test_business_rule_file_requires_rules_list(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("name: invalid\n", encoding="utf-8")

    with pytest.raises(ValueError, match="top-level rules list"):
        load_business_rules(path)
