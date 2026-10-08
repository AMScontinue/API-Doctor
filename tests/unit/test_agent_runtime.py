from datetime import datetime
from pathlib import Path

import pytest

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


def _delete_workflow(catalog):
    candidate = WorkflowCandidate.model_validate(
        {
            "workflow_id": "create_delete_user",
            "purpose": "create then delete one user",
            "confidence": "high",
            "reasoning": "delete the resource created by the first step",
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
                    "step_id": "delete",
                    "operation_id": "deleteUser",
                    "purpose": "delete user",
                    "bind": [
                        {"location": "path", "name": "id", "variable": "user_id"}
                    ],
                },
            ],
        }
    )
    workflow = validate_workflows([candidate], catalog).reviews[0].compiled_workflow
    assert workflow is not None and workflow.requires_approval
    return workflow


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


def test_artifact_store_refuses_to_overwrite_existing_run(tmp_path):
    output = tmp_path / "agent-run"
    output.mkdir()
    trajectory = output / "trajectory.jsonl"
    trajectory.write_text("existing event\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        AgentArtifactStore(output)

    assert trajectory.read_text(encoding="utf-8") == "existing event\n"


def test_delete_action_pauses_with_a_persisted_approval_request(tmp_path):
    catalog, plan, _ = _inputs()
    workflow = _delete_workflow(catalog)
    action = PlannedAction(
        kind="workflow",
        item_id=workflow.workflow_id,
        priority=90,
        reason="lifecycle cleanup",
        estimated_requests=2,
    )
    store = AgentArtifactStore(tmp_path / "approval-pause")

    run = run_agent_actions(
        _state(catalog, plan, [action]),
        plan,
        [workflow],
        catalog,
        _config(),
        approval_checkpoint=store.checkpoint_approval,
    )

    assert run.state.phase == "awaiting_approval"
    assert run.state.pending_actions == [action]
    assert run.state.completed_actions == []
    assert run.state.pending_approval.targets == ["DELETE /users/{id}"]
    checkpoint = store.checkpoint_path.read_text(encoding="utf-8")
    assert '"approval_decision": null' in checkpoint
    assert '"event": "approval_requested"' in store.trajectory_path.read_text()


def test_human_rejection_blocks_delete_without_sending_http(monkeypatch, tmp_path):
    catalog, plan, _ = _inputs()
    workflow = _delete_workflow(catalog)
    action = PlannedAction(
        kind="workflow",
        item_id=workflow.workflow_id,
        priority=90,
        reason="lifecycle cleanup",
        estimated_requests=2,
    )
    store = AgentArtifactStore(tmp_path / "approval-rejected")
    monkeypatch.setattr(
        "api_doctor.execution.workflow_runner.execute_case",
        lambda *args, **kwargs: pytest.fail("HTTP must not run after rejection"),
    )

    run = run_agent_actions(
        _state(catalog, plan, [action]),
        plan,
        [workflow],
        catalog,
        _config(),
        approval_callback=lambda request: False,
        approval_checkpoint=store.checkpoint_approval,
    )

    assert run.state.phase == "finished"
    assert run.state.observations[0].outcome == "blocked"
    assert run.state.observations[0].request_count == 0
    assert run.state.remaining_test_budget == 5
    assert '"approved": false' in store.trajectory_path.read_text()


def test_human_approval_is_scoped_to_one_validated_action(monkeypatch):
    catalog, plan, _ = _inputs()
    workflow = _delete_workflow(catalog)
    action = PlannedAction(
        kind="workflow",
        item_id=workflow.workflow_id,
        priority=90,
        reason="lifecycle cleanup",
        estimated_requests=2,
    )

    def fake_execute(case, policy, operation=None):
        assert policy.approve_delete is True
        body = {"id": 42, "name": "Ada", "email": "ada@example.test"}
        return CaseResult(
            case_id=case.case_id,
            outcome="passed",
            actual_status=case.assertions[0].allowed[0],
            expected_statuses=case.assertions[0].allowed,
            response_body=body if operation.operation_id == "createUser" else None,
            request_url="http://127.0.0.1:18080" + case.request.path_template,
        )

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_agent_actions(
        _state(catalog, plan, [action]),
        plan,
        [workflow],
        catalog,
        _config(),
        approval_callback=lambda request: True,
    )

    assert run.state.phase == "finished"
    assert run.state.observations[0].outcome == "passed"
    assert run.state.approved_action_ids == {"workflow:create_delete_user"}


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


def test_passed_diagnostic_action_returns_to_analyzer(monkeypatch):
    catalog, plan, workflows = _inputs()
    monkeypatch.setattr("api_doctor.agent.runtime.execute_case", _fake_pass)
    diagnostic = PlannedAction(
        kind="test_case",
        item_id=plan.cases[0].case.case_id,
        priority=100,
        reason="collect evidence for an active hypothesis",
        estimated_requests=1,
        origin="diagnostic",
    )

    run = run_agent_actions(
        _state(catalog, plan, [diagnostic]),
        plan,
        workflows,
        catalog,
        _config(),
    )

    assert run.state.observations[0].outcome == "passed"
    assert run.state.phase == "analyzing"
    assert run.state.completed_actions == [diagnostic]


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


def test_diagnostic_case_may_repeat_an_executed_fingerprint_only_once(monkeypatch):
    catalog, plan, workflows = _inputs()
    monkeypatch.setattr("api_doctor.agent.runtime.execute_case", _fake_pass)
    planned = plan.cases[0]
    diagnostic = PlannedAction(
        kind="test_case",
        item_id=planned.case.case_id,
        priority=100,
        reason="one evidence-backed replay",
        estimated_requests=1,
        origin="diagnostic",
        allow_repeat=True,
    )
    state = _state(catalog, plan, [diagnostic, diagnostic.model_copy()], tests=2, steps=2)
    state.executed_fingerprints.add(planned.request_fingerprint)

    first = run_agent_actions(state, plan, workflows, catalog, _config())
    resumed = first.state.model_copy(update={"phase": "executing"})
    second = run_agent_actions(resumed, plan, workflows, catalog, _config())

    assert first.state.observations[-1].outcome == "passed"
    assert planned.request_fingerprint in first.state.repeated_fingerprints
    assert second.state.observations[-1].outcome == "blocked"


def test_independent_workflows_may_use_the_same_baseline_fixture(monkeypatch):
    catalog, plan, workflows = _inputs()
    second = workflows[0].model_copy(update={"workflow_id": "order_create_read_again"})
    actions = [
        PlannedAction(
            kind="workflow",
            item_id=workflow.workflow_id,
            priority=100 - index,
            reason="independent lifecycle coverage",
            estimated_requests=2,
        )
        for index, workflow in enumerate([workflows[0], second])
    ]
    created_bodies = []

    def fake_execute(case, policy, operation=None):
        del policy
        if operation.operation_id == "createOrder":
            created_bodies.append(case.request.body)
            order_id = len(created_bodies)
            return CaseResult(
                case_id=case.case_id,
                outcome="passed",
                actual_status=201,
                expected_statuses=case.assertions[0].allowed,
                response_body={"id": order_id},
                request_url="http://127.0.0.1:18080/orders",
            )
        order_id = case.request.path_parameters["id"]
        return CaseResult(
            case_id=case.case_id,
            outcome="passed",
            actual_status=200,
            expected_statuses=case.assertions[0].allowed,
            response_body={"id": order_id},
            request_url=f"http://127.0.0.1:18080/orders/{order_id}",
        )

    monkeypatch.setattr("api_doctor.execution.workflow_runner.execute_case", fake_execute)
    run = run_agent_actions(
        _state(catalog, plan, actions, tests=4, steps=4),
        plan,
        [workflows[0], second],
        catalog,
        _config(max_tests=4),
    )

    assert [item.outcome for item in run.state.observations] == ["passed", "passed"]
    assert len(created_bodies) == 2
    assert created_bodies[0] == created_bodies[1]


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
