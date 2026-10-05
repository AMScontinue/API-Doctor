from pathlib import Path

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


def test_rejects_duplicate_bindings_to_same_parameter():
    steps = _workflow().model_dump(mode="json")["steps"]
    steps[1]["bind"].append(dict(steps[1]["bind"][0]))
    review = _validate([_workflow(steps=steps)]).reviews[0]

    assert review.verdict == "rejected"
    assert "more than once" in review.errors[0]


def test_rejects_duplicate_workflow_ids():
    result = _validate([_workflow(), _workflow()])

    assert result.summary()["rejected"] == 2
    assert all("duplicate workflow_id" in review.errors[0] for review in result.reviews)
