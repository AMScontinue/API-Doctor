import json
from pathlib import Path


from api_doctor.generation.workflow_discovery import (
    build_catalog_context,
    discover_workflows,
)
from api_doctor.generation.rules import load_business_rules
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelResponse
from api_doctor.openapi import load_openapi


ROOT = Path(__file__).resolve().parents[2]


class FakeGateway:
    def __init__(self, discovery: WorkflowDiscovery) -> None:
        self.discovery = discovery
        self.prompt = None

    def complete(self, prompt, *, response_model, trace_label):
        self.prompt = prompt
        assert response_model is WorkflowDiscovery
        assert trace_label == "workflow-discovery"
        return ModelResponse(
            value=self.discovery,
            telemetry=ModelCallTelemetry(calls=1, duration_ms=5),
        )


def _discovery() -> WorkflowDiscovery:
    return WorkflowDiscovery.model_validate(
        {
            "workflows": [
                {
                    "workflow_id": "create_then_get_user",
                    "purpose": "Retrieve the newly created user",
                    "confidence": "high",
                    "reasoning": "The create response and lookup parameter share a user identifier",
                    "steps": [
                        {
                            "step_id": "create",
                            "operation_id": "createUser",
                            "purpose": "Create a user",
                            "extract": [
                                {
                                    "variable": "user_id",
                                    "response_expression": "$response.body#/id",
                                }
                            ],
                        },
                        {
                            "step_id": "get",
                            "operation_id": "getUser",
                            "purpose": "Read the same user",
                            "bind": [
                                {
                                    "location": "path",
                                    "name": "id",
                                    "variable": "user_id",
                                }
                            ],
                        },
                    ],
                }
            ],
            "ambiguities": [],
        }
    )


def test_catalog_context_excludes_openapi_links():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    rendered = json.dumps(build_catalog_context(catalog))

    assert '"operation_id": "createUser"' in rendered
    assert '"operation_id": "getUser"' in rendered
    assert '"links"' not in rendered
    assert "$response.body#/id" not in rendered


def test_discovery_returns_typed_non_executable_proposals():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    rules = load_business_rules(ROOT / "examples/buggy-shop/rules.yaml")
    gateway = FakeGateway(_discovery())

    response = discover_workflows(catalog, gateway, rules)

    assert response.value.workflows[0].steps[1].bind[0].variable == "user_id"
    assert gateway.prompt["catalog"] == build_catalog_context(catalog)
    assert {item["id"] for item in gateway.prompt["business_rules"]} >= {
        "unique-user-email",
        "repeated-order-status-conflict",
    }


def test_discovery_parses_cross_step_errors_for_independent_validation():
    payload = _discovery().model_dump(mode="json")
    payload["workflows"][0]["steps"] = list(reversed(payload["workflows"][0]["steps"]))

    discovery = WorkflowDiscovery.model_validate(payload)

    assert discovery.workflows[0].steps[0].step_id == "get"


def test_discovery_deterministically_limits_oversized_model_batch():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    discovery = _discovery()
    workflows = [
        discovery.workflows[0].model_copy(update={"workflow_id": f"workflow_{index}"})
        for index in range(11)
    ]
    gateway = FakeGateway(discovery.model_copy(update={"workflows": workflows}))

    response = discover_workflows(catalog, gateway)

    assert len(response.value.workflows) == 10
    assert response.value.workflows[-1].workflow_id == "workflow_9"
    assert response.value.ambiguities[-1] == (
        "Host omitted 1 workflow proposal(s) beyond the limit of 10."
    )
