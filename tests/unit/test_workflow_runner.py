from datetime import datetime
from pathlib import Path

from api_doctor.execution.models import RunConfig
from api_doctor.execution.safety import SafetyViolation
from api_doctor.execution.workflow_runner import run_workflow
from api_doctor.generation.workflow_models import WorkflowCandidate
from api_doctor.openapi import load_openapi
from api_doctor.reporting.report import save_workflow_run_artifacts
from api_doctor.testspec.models import TestResult as CaseResult
from api_doctor.workflows.validation import (
    CompiledExtraction,
    ExtractionSchema,
    validate_workflows,
)


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _compiled_workflow():
    candidate = WorkflowCandidate.model_validate(
        {
            "workflow_id": "user_lifecycle",
            "purpose": "create, read, and delete one user",
            "confidence": "high",
            "reasoning": "the create response id feeds both path parameters",
            "steps": [
                {
                    "step_id": "create",
                    "operation_id": "createUser",
                    "purpose": "create user",
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
                    "purpose": "read user",
                    "bind": [{"location": "path", "name": "id", "variable": "user_id"}],
                },
                {
                    "step_id": "delete",
                    "operation_id": "deleteUser",
                    "purpose": "delete user",
                    "bind": [{"location": "path", "name": "id", "variable": "user_id"}],
                },
            ],
        }
    )
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    compiled = validate_workflows([candidate], catalog).reviews[0].compiled_workflow
    assert compiled is not None
    return catalog, compiled


def _config(*, approve_delete: bool = True, max_tests: int = 3) -> RunConfig:
    return RunConfig(
        base_url="http://127.0.0.1:18080",
        seed=7,
        time_anchor=ANCHOR,
        max_tests=max_tests,
        approve_delete=approve_delete,
    )


def _passed(case, status, body=None):
    return CaseResult(
        case_id=case.case_id,
        outcome="passed",
        actual_status=status,
        expected_statuses=case.assertions[0].allowed,
        response_body=body,
        request_url="http://127.0.0.1:18080" + case.request.path_template,
    )


def test_executes_steps_injects_variable_and_saves_audit_artifacts(monkeypatch, tmp_path):
    catalog, workflow = _compiled_workflow()
    observed = []

    def fake_execute(case, policy, operation=None):
        del policy
        observed.append((operation.operation_id, dict(case.request.path_parameters)))
        if operation.operation_id == "createUser":
            return _passed(
                case,
                201,
                {"id": 42, "name": "Ada", "email": "ada@example.test"},
            )
        if operation.operation_id == "getUser":
            return _passed(
                case,
                200,
                {"id": 42, "name": "Ada", "email": "ada@example.test"},
            )
        return _passed(case, 204)

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_workflow(workflow, catalog, _config())
    artifacts = save_workflow_run_artifacts(run, tmp_path / "workflow-run")

    assert observed == [
        ("createUser", {}),
        ("getUser", {"id": 42}),
        ("deleteUser", {"id": 42}),
    ]
    assert run.metrics.passed == 3
    assert run.variables[0].value == 42
    assert run.records[1].compiled_case.dependencies == ["WF-user_lifecycle-create"]
    assert artifacts["json"].exists()
    assert "## Steps" in artifacts["markdown"].read_text()
    assert len(artifacts["trajectory"].read_text().splitlines()) == 3


def test_delete_is_blocked_without_approval_after_safe_prefix(monkeypatch):
    catalog, workflow = _compiled_workflow()

    def fake_execute(case, policy, operation=None):
        try:
            policy.resolve_url(case)
        except SafetyViolation as error:
            return CaseResult(
                case_id=case.case_id,
                outcome="blocked",
                actual_status=None,
                expected_statuses=case.assertions[0].allowed,
                error=str(error),
            )
        status = case.assertions[0].allowed[0]
        body = (
            {"id": 9, "name": "Ada", "email": "ada@example.test"}
            if operation.operation_id in {"createUser", "getUser"}
            else None
        )
        return _passed(case, status, body)

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_workflow(workflow, catalog, _config(approve_delete=False))

    assert run.metrics.passed == 2
    assert run.metrics.blocked == 1
    assert run.records[2].result.outcome == "blocked"
    assert "approve-delete" in run.records[2].reason


def test_invalid_extracted_value_halts_downstream_steps(monkeypatch):
    catalog, workflow = _compiled_workflow()

    def fake_execute(case, policy, operation=None):
        del policy, operation
        return _passed(
            case,
            201,
            {"id": "not-an-integer", "name": "Ada", "email": "ada@example.test"},
        )

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_workflow(workflow, catalog, _config())

    assert run.metrics.errors == 1
    assert run.metrics.blocked == 2
    assert "violates its response schema" in run.records[0].reason
    assert run.records[1].compiled_case is None


def test_step_extractions_are_committed_atomically(monkeypatch):
    catalog, workflow = _compiled_workflow()
    workflow.steps[0].extract.append(
        CompiledExtraction(
            variable="user_email",
            response_expression="$response.body#/email",
            schemas=[ExtractionSchema(status_code=201, json_schema={"type": "string"})],
        )
    )

    def fake_execute(case, policy, operation=None):
        del policy, operation
        return _passed(case, 201, {"id": 7})

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_workflow(workflow, catalog, _config())

    assert run.variables == []
    assert run.metrics.errors == 1
    assert "does not exist" in run.records[0].reason


def test_budget_halts_unexecuted_workflow_suffix(monkeypatch):
    catalog, workflow = _compiled_workflow()

    def fake_execute(case, policy, operation=None):
        del policy
        body = (
            {"id": 5, "name": "Ada", "email": "ada@example.test"}
            if operation.operation_id == "createUser"
            else {"id": 5, "name": "Ada", "email": "ada@example.test"}
        )
        return _passed(case, case.assertions[0].allowed[0], body)

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_workflow(workflow, catalog, _config(max_tests=2))

    assert run.metrics.passed == 2
    assert run.metrics.blocked == 1
    assert run.records[2].reason == "max_tests budget exhausted"
