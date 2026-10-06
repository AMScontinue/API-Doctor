from datetime import datetime
from pathlib import Path

from api_doctor.agent.models import AgentState, PlannedAction
from api_doctor.agent.persistence import AgentArtifactStore
from api_doctor.agent.runtime import AgentRuntimeError, run_agent_actions
from api_doctor.execution.models import RunConfig
from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.generation.workflow_models import WorkflowCandidate
from api_doctor.openapi import load_openapi
from api_doctor.testspec.fusion import fuse_test_cases
from api_doctor.testspec.models import TestResult as CaseResult
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate
from api_doctor.workflows.validation import validate_workflows


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _inputs():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    candidate = next(
        item
        for item in generate_schema_candidates(catalog)
        if item.rule_id == "schema:createUser:missing-email"
    )
    case = compile_openapi_candidate(
        candidate,
        catalog,
        case_number=1,
        seed=7,
        anchor=ANCHOR,
    )
    plan = fuse_test_cases([case])
    workflow_candidate = WorkflowCandidate.model_validate(
        {
            "workflow_id": "order_create_read",
            "purpose": "create then read one order",
            "confidence": "high",
            "reasoning": "create response id matches read path id",
            "steps": [
                {
                    "step_id": "create",
                    "operation_id": "createOrder",
                    "purpose": "create order",
                    "extract": [
                        {
                            "variable": "order_id",
                            "response_expression": "$response.body#/id",
                        }
                    ],
                },
                {
                    "step_id": "read",
                    "operation_id": "getOrder",
                    "purpose": "read order",
                    "bind": [{"location": "path", "name": "id", "variable": "order_id"}],
                },
            ],
        }
    )
    workflow = validate_workflows([workflow_candidate], catalog).reviews[0].compiled_workflow
    assert workflow is not None
    return catalog, plan, [workflow]


def _state(catalog, plan, actions=None, *, tests=5, steps=5):
    selected = actions or [
        PlannedAction(
            kind="workflow",
            item_id="order_create_read",
            priority=100,
            reason="dependency coverage",
            estimated_requests=2,
        ),
        PlannedAction(
            kind="test_case",
            item_id=plan.cases[0].case.case_id,
            priority=80,
            reason="contract negative",
            estimated_requests=1,
        ),
    ]
    return AgentState(
        spec_fingerprint=catalog.fingerprint,
        phase="executing",
        strategy="test workflow then contract negative",
        pending_actions=selected,
        remaining_test_budget=tests,
        remaining_step_budget=steps,
    )


def _config(max_tests=5):
    return RunConfig(
        base_url="http://127.0.0.1:18080",
        seed=7,
        time_anchor=ANCHOR,
        max_tests=max_tests,
    )


def _fake_pass(case, policy, operation=None):
    del policy
    status = case.assertions[0].allowed[0]
    if operation.operation_id == "createOrder":
        body = {"id": 42}
    elif operation.operation_id == "getOrder":
        body = {"id": 42}
    else:
        body = {"error": "rejected"}
    return CaseResult(
        case_id=case.case_id,
        outcome="passed",
        actual_status=status,
        expected_statuses=case.assertions[0].allowed,
        response_body=body,
        request_url="http://127.0.0.1:18080" + case.request.path_template,
    )


def test_executes_mixed_queue_updates_state_and_checkpoints(monkeypatch, tmp_path):
    catalog, plan, workflows = _inputs()
    monkeypatch.setattr("api_doctor.agent.runtime.execute_case", _fake_pass)
    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", _fake_pass)
    store = AgentArtifactStore(tmp_path / "agent-run")

    run = run_agent_actions(
        _state(catalog, plan),
        plan,
        workflows,
        catalog,
        _config(),
        checkpoint=store.checkpoint,
    )
    artifacts = store.finalize(run)

    assert run.state.phase == "finished"
    assert run.state.termination_reason == "pending action queue exhausted"
    assert len(run.state.completed_actions) == 2
    assert [item.request_count for item in run.state.observations] == [2, 1]
    assert run.state.remaining_test_budget == 2
    assert run.state.remaining_step_budget == 2
    assert run.state.variables[0].name == "order_id"
    assert run.state.variables[0].value == 42
    assert len(run.state.executed_fingerprints) == 3
    assert len(artifacts["trajectory"].read_text().splitlines()) == 2
    assert artifacts["checkpoint"].exists()
    assert "## Observations" in artifacts["report"].read_text()


def test_failure_pauses_queue_for_future_analyzer(monkeypatch):
    catalog, plan, workflows = _inputs()

    def fake_failure(case, policy, operation=None):
        del policy, operation
        return CaseResult(
            case_id=case.case_id,
            outcome="failed",
            actual_status=201,
            expected_statuses=case.assertions[0].allowed,
            request_url="http://127.0.0.1:18080/users",
            violations=["status 201 is not expected"],
        )

    monkeypatch.setattr("api_doctor.agent.runtime.execute_case", fake_failure)
    case_action = PlannedAction(
        kind="test_case",
        item_id=plan.cases[0].case.case_id,
        priority=100,
        reason="trigger a finding",
        estimated_requests=1,
    )
    workflow_action = PlannedAction(
        kind="workflow",
        item_id="order_create_read",
        priority=90,
        reason="leave pending",
        estimated_requests=2,
    )

    run = run_agent_actions(
        _state(catalog, plan, [case_action, workflow_action]),
        plan,
        workflows,
        catalog,
        _config(),
    )

    assert run.state.phase == "analyzing"
    assert len(run.state.completed_actions) == 1
    assert run.state.pending_actions == [workflow_action]
    assert run.state.observations[0].outcome == "failed"


def test_duplicate_case_is_blocked_without_spending_request_budget(monkeypatch):
    catalog, plan, workflows = _inputs()
    calls = 0

    def counted(case, policy, operation=None):
        nonlocal calls
        calls += 1
        return _fake_pass(case, policy, operation)

    monkeypatch.setattr("api_doctor.agent.runtime.execute_case", counted)
    action = PlannedAction(
        kind="test_case",
        item_id=plan.cases[0].case.case_id,
        priority=80,
        reason="same action twice",
        estimated_requests=1,
    )
    run = run_agent_actions(
        _state(catalog, plan, [action, action.model_copy()], tests=2, steps=2),
        plan,
        workflows,
        catalog,
        _config(),
    )

    assert calls == 1
    assert [item.outcome for item in run.state.observations] == ["passed", "blocked"]
    assert run.state.remaining_test_budget == 1
    assert run.state.remaining_step_budget == 0


def test_tampered_action_cost_is_rejected_before_http(monkeypatch):
    catalog, plan, workflows = _inputs()
    called = False

    def should_not_execute(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("HTTP executor should not be called")

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", should_not_execute)
    action = PlannedAction(
        kind="workflow",
        item_id="order_create_read",
        priority=100,
        reason="under-report workflow cost",
        estimated_requests=1,
    )
    run = run_agent_actions(
        _state(catalog, plan, [action]),
        plan,
        workflows,
        catalog,
        _config(),
    )

    assert called is False
    assert run.state.phase == "finished"
    assert run.state.termination_reason == "trusted action validation failed"
    assert run.state.observations[0].outcome == "error"


def test_insufficient_budget_finishes_without_consuming_action(monkeypatch):
    catalog, plan, workflows = _inputs()
    action = PlannedAction(
        kind="workflow",
        item_id="order_create_read",
        priority=100,
        reason="needs two requests",
        estimated_requests=2,
    )
    run = run_agent_actions(
        _state(catalog, plan, [action], tests=1, steps=3),
        plan,
        workflows,
        catalog,
        _config(),
    )

    assert run.records == []
    assert run.state.pending_actions == [action]
    assert run.state.termination_reason == "remaining request budget cannot execute the next action"


def test_rejects_state_created_for_a_different_openapi_document():
    catalog, plan, workflows = _inputs()
    state = _state(catalog, plan)
    state.spec_fingerprint = "different-spec"

    try:
        run_agent_actions(state, plan, workflows, catalog, _config())
    except AgentRuntimeError as error:
        assert "fingerprint" in str(error)
    else:
        raise AssertionError("mismatched AgentState was accepted")
