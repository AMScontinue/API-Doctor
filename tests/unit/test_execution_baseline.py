from datetime import datetime
from pathlib import Path

from api_doctor.execution.runner import run_baseline
from api_doctor.execution.response_validation import validate_response
from api_doctor.execution.safety import ExecutionPolicy, SafetyViolation
from api_doctor.openapi import load_openapi
from api_doctor.reporting.report import save_run_artifacts
from api_doctor.testspec.models import (
    Assertion,
    CompiledRequest,
    CompiledTestCase,
    TestResult as CaseResult,
)


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def test_deterministic_runner_executes_budget_and_writes_redacted_artifacts(
    tmp_path, monkeypatch
):
    def fake_execute(case, operation, policy):
        del operation
        expected = case.assertions[0].allowed
        return CaseResult(
            case_id=case.case_id,
            outcome="passed",
            actual_status=expected[0],
            expected_statuses=expected,
            request_url=policy.resolve_url(case),
            response_headers={"authorization": "secret"},
        )

    monkeypatch.setattr("api_doctor.execution.runner.execute_openapi_case", fake_execute)
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    run = run_baseline(
        catalog,
        base_url="http://127.0.0.1:18080",
        seed=7,
        anchor=ANCHOR,
        max_tests=6,
    )
    paths = save_run_artifacts(run, tmp_path / "run")

    assert run.metrics.operations == 5
    assert run.metrics.candidates == 22
    assert run.metrics.executed == 6
    assert run.metrics.blocked == 16
    assert run.metrics.passed + run.metrics.failed + run.metrics.errors == 6
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    assert len(paths["trajectory"].read_text().splitlines()) == 22
    assert "## Summary" in paths["markdown"].read_text()
    assert "secret" not in paths["json"].read_text()


def test_execution_policy_rejects_bad_base_urls_and_cross_origin():
    try:
        ExecutionPolicy("file:///tmp/service")
    except SafetyViolation as error:
        assert "http(s)" in str(error)
    else:
        raise AssertionError("unsafe scheme was accepted")

    policy = ExecutionPolicy("http://127.0.0.1:8080")
    try:
        policy.ensure_same_origin("http://example.test/redirect")
    except SafetyViolation as error:
        assert "escaped" in str(error)
    else:
        raise AssertionError("cross-origin redirect was accepted")


def test_response_validation_checks_status_content_type_and_schema():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    operation = catalog.get("createUser")

    valid = validate_response(
        operation,
        status=201,
        headers={"content-type": "application/json; charset=utf-8"},
        body={"id": 1, "name": "Ada", "email": "ada@example.test"},
    )
    invalid = validate_response(
        operation,
        status=201,
        headers={"content-type": "text/plain"},
        body={"name": "Ada"},
    )
    undeclared = validate_response(
        operation,
        status=500,
        headers={"content-type": "application/json"},
        body={"error": "boom"},
    )

    assert valid == []
    assert any("Content-Type" in item for item in invalid)
    assert any("response schema" in item for item in invalid)
    assert undeclared == ["status 500 is not declared by the OpenAPI operation"]


def test_delete_requires_explicit_approval():
    case = CompiledTestCase(
        case_id="TC-DELETE",
        rule_id="delete",
        operation_id="deleteItem",
        purpose="delete item",
        request=CompiledRequest(method="DELETE", path_template="/items/1"),
        assertions=[Assertion(allowed=[204], source="openapi", evidence_ref="#/responses")],
        risk="requires_approval",
        seed=1,
        time_anchor=ANCHOR,
    )

    try:
        ExecutionPolicy("http://127.0.0.1:8080").resolve_url(case)
    except SafetyViolation as error:
        assert "approve-delete" in str(error)
    else:
        raise AssertionError("DELETE was allowed without approval")

    assert ExecutionPolicy(
        "http://127.0.0.1:8080", approve_delete=True
    ).resolve_url(case).endswith("/items/1")
