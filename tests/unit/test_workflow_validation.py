from pathlib import Path

import pytest

from api_doctor.generation.workflow_models import WorkflowCandidate
from api_doctor.openapi import load_openapi
from api_doctor.workflows.validation import validate_workflows


ROOT = Path(__file__).resolve().parents[2]


def _workflow(**overrides) -> WorkflowCandidate:
    payload = {
        "workflow_id": "create_read_delete_user",
        "purpose": "exercise a user lifecycle",
        "confidence": "high",
        "reasoning": "the create response id matches consumer path parameters",
        "steps": [
            {
                "step_id": "create",
                "operation_id": "createUser",
                "purpose": "create a user",
                "extract": [
                    {
                        "variable": "user_id",
                        "response_expression": "$response.body#/id",
                    }
                ],
            },
            {
                "step_id": "read",
                "operation_id": "getUser",
                "purpose": "read the created user",
                "bind": [
                    {
                        "location": "path",
                        "name": "id",
                        "variable": "user_id",
                    }
                ],
            },
            {
                "step_id": "delete",
                "operation_id": "deleteUser",
                "purpose": "delete the created user",
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
    payload.update(overrides)
    return WorkflowCandidate.model_validate(payload)


def _validate(workflows: list[WorkflowCandidate]):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    return validate_workflows(workflows, catalog)


def test_compiles_grounded_lifecycle_and_marks_delete_for_approval():
    review = _validate([_workflow()]).reviews[0]

    assert review.verdict == "accepted"
    assert review.compiled_workflow is not None
    assert review.compiled_workflow.requires_approval is True
    extraction = review.compiled_workflow.steps[0].extract[0]
    assert [schema.status_code for schema in extraction.schemas] == [201]
    assert extraction.schemas[0].json_schema["type"] == "integer"
    assert review.compiled_workflow.steps[1].bind[0].target_schema["type"] == "integer"


def test_rejects_unknown_operation_without_aborting_valid_workflow():
    invalid_steps = _workflow().model_dump(mode="json")["steps"]
    invalid_steps[1]["operation_id"] = "inventedOperation"
    result = _validate(
        [_workflow(workflow_id="invalid_workflow", steps=invalid_steps), _workflow()]
    )

    assert result.summary() == {
        "total": 2,
        "accepted": 1,
        "rejected": 1,
        "compiled": 1,
        "requiring_approval": 1,
    }
    assert "unknown operation" in result.reviews[0].errors[0]


def test_rejects_variable_use_before_extraction_without_aborting_batch():
    invalid_steps = list(reversed(_workflow().model_dump(mode="json")["steps"]))
    result = _validate(
        [_workflow(workflow_id="invalid_order", steps=invalid_steps), _workflow()]
    )

    assert result.summary()["accepted"] == 1
    assert result.summary()["rejected"] == 1
    assert "used before it is extracted" in result.reviews[0].errors[0]


def test_rejects_pointer_missing_from_success_responses():
    steps = _workflow().model_dump(mode="json")["steps"]
    steps[0]["extract"][0]["response_expression"] = "$response.body#/error"
    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "absent from every declared 2xx response schema" in review.errors[0]


def test_rejects_unknown_parameter_location():
    steps = _workflow().model_dump(mode="json")["steps"]
    steps[1]["bind"][0]["location"] = "query"
    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "unknown query parameter" in review.errors[0]


def test_rejects_incompatible_extraction_and_parameter_types():
    steps = _workflow().model_dump(mode="json")["steps"]
    steps[0]["extract"][0]["response_expression"] = "$response.body#/email"
    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "not type-compatible" in review.errors[0]


def test_compiles_response_variable_binding_to_request_body_field():
    steps = _workflow().model_dump(mode="json")["steps"][:2]
    steps[0]["extract"].append(
        {"variable": "user_email", "response_expression": "$response.body#/email"}
    )
    steps[1] = {
        "step_id": "create-again",
        "operation_id": "createUser",
        "purpose": "reuse the created email",
        "bind": [{"location": "body", "name": "email", "variable": "user_email"}],
    }

    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "accepted"
    binding = review.compiled_workflow.steps[1].bind[0]
    assert binding.location == "body"
    assert binding.target_schema["format"] == "email"


def test_rejects_unknown_request_body_binding():
    steps = _workflow().model_dump(mode="json")["steps"][:2]
    steps[1]["operation_id"] = "createUser"
    steps[1]["bind"][0] = {
        "location": "body",
        "name": "missing_field",
        "variable": "user_id",
    }

    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "unknown request body field" in review.errors[0]


def test_compiles_declared_error_expectation_for_intentional_repeat():
    steps = _workflow().model_dump(mode="json")["steps"][:2]
    steps[0]["extract"].append(
        {"variable": "user_email", "response_expression": "$response.body#/email"}
    )
    steps[1] = {
        "step_id": "create-duplicate",
        "operation_id": "createUser",
        "purpose": "verify unique email",
        "expected_status_codes": [409],
        "repeat": "once",
        "bind": [{"location": "body", "name": "email", "variable": "user_email"}],
    }

    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "accepted"
    compiled = review.compiled_workflow.steps[1]
    assert compiled.expected_status_codes == [409]
    assert compiled.repeat == "once"


def test_rejects_undeclared_expected_status():
    steps = _workflow().model_dump(mode="json")["steps"][:2]
    steps[1]["expected_status_codes"] = [418]

    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "undeclared response status" in review.errors[0]


def test_rejects_duplicate_bindings_to_same_parameter():
    steps = _workflow().model_dump(mode="json")["steps"]
    steps[1]["bind"].append(dict(steps[1]["bind"][0]))
    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "more than once" in review.errors[0]


def test_compiles_schema_valid_constant_request_body_assignment():
    steps = _workflow().model_dump(mode="json")["steps"][:2]
    steps[1] = {
        "step_id": "confirm-order",
        "operation_id": "updateOrderStatus",
        "purpose": "move the order to confirmed",
        "bind": [
            {"location": "path", "name": "id", "variable": "user_id"}
        ],
        "assign": [{"location": "body", "name": "status", "value": "confirmed"}],
    }

    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "accepted"
    assignment = review.compiled_workflow.steps[1].assign[0]
    assert assignment.model_dump() == {
        "location": "body",
        "name": "status",
        "value": "confirmed",
    }


def test_rejects_invalid_or_conflicting_constant_assignment():
    steps = _workflow().model_dump(mode="json")["steps"][:2]
    steps[1] = {
        "step_id": "confirm-order",
        "operation_id": "updateOrderStatus",
        "purpose": "move the order to an invalid state",
        "bind": [
            {"location": "path", "name": "id", "variable": "user_id"}
        ],
        "assign": [
            {"location": "path", "name": "id", "value": 1},
            {"location": "body", "name": "status", "value": "invented"},
        ],
    }

    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert any("assigns or binds" in error for error in review.errors)
    assert any("violates target schema" in error for error in review.errors)


def test_rejects_duplicate_workflow_ids():
    result = _validate([_workflow(), _workflow()])

    assert result.summary()["rejected"] == 2
    assert all("duplicate workflow_id" in review.errors[0] for review in result.reviews)


def test_workflow_candidate_rejects_single_step():
    payload = _workflow().model_dump(mode="json")
    payload["steps"] = payload["steps"][:1]

    with pytest.raises(ValueError, match="at least 2 items"):
        WorkflowCandidate.model_validate(payload)


def test_rejects_missing_or_schema_invalid_working_memory_variable():
    payload = _workflow().model_dump(mode="json")
    read_step = {
        "step_id": "read",
        "operation_id": "getOrder",
        "purpose": "read an existing order",
        "expected_status_codes": [200],
        "bind": [
            {
                "location": "path",
                "name": "id",
                "variable": "order_id",
                "source": "working_memory",
            }
        ],
    }
    payload["steps"] = [
        read_step,
        {
            **read_step,
            "step_id": "read-again",
            "repeat": "once",
        },
    ]
    candidate = WorkflowCandidate.model_validate(payload)
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")

    missing = validate_workflows([candidate], catalog).reviews[0]
    invalid = validate_workflows(
        [candidate], catalog, working_memory={"order_id": "not-an-integer"}
    ).reviews[0]

    assert "unavailable working-memory variable" in missing.errors[0]
    assert "violates target schema" in invalid.errors[0]
