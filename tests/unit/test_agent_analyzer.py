import json
from datetime import datetime
from pathlib import Path

from api_doctor.agent.analyzer import (
    apply_analysis_decision,
    apply_analyzer_failure,
    build_analysis_prompt,
)
from api_doctor.agent.models import (
    AnalysisDecision,
    AgentActionRecord,
    AgentHypothesis,
    AgentObservation,
    AgentState,
    AgentVariable,
    PlannedAction,
)
from api_doctor.agent.persistence import AgentArtifactStore
from api_doctor.generation.workflow_models import WorkflowCandidate
from api_doctor.openapi import load_openapi


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _apply(state, decision, catalog):
    return apply_analysis_decision(
        state,
        decision,
        catalog,
        seed=7,
        anchor=ANCHOR,
    )


def _state(catalog, *, tests=4, steps=5, pending=True):
    actions = (
        [
            PlannedAction(
                kind="test_case",
                item_id="TC-pending",
                priority=50,
                reason="continue baseline coverage",
                estimated_requests=1,
            )
        ]
        if pending
        else []
    )
    return AgentState(
        spec_fingerprint=catalog.fingerprint,
        phase="analyzing",
        strategy="bounded diagnosis",
        pending_actions=actions,
        remaining_test_budget=tests,
        remaining_step_budget=steps,
    )


def _failure():
    action = PlannedAction(
        kind="test_case",
        item_id="TC-failed",
        priority=90,
        reason="exercise a contract",
        estimated_requests=1,
    )
    return AgentActionRecord(
        action=action,
        observation=AgentObservation(
            action_kind="test_case",
            action_id="TC-failed",
            outcome="failed",
            evidence_ref="trajectory:actions/1",
            summary="createUser returned 201; expected=[400]",
            request_count=1,
            violations=["status 201 is not expected"],
        ),
    )


def _diagnostic_workflow():
    return WorkflowCandidate.model_validate(
        {
            "workflow_id": "diagnose_duplicate_email",
            "purpose": "check whether duplicate emails are rejected consistently",
            "confidence": "medium",
            "reasoning": "a controlled repeat distinguishes uniqueness enforcement",
            "steps": [
                {
                    "step_id": "create",
                    "operation_id": "createUser",
                    "purpose": "create one user",
                    "extract": [
                        {
                            "variable": "created_email",
                            "response_expression": "$response.body#/email",
                        }
                    ],
                },
                {
                    "step_id": "repeat",
                    "operation_id": "createUser",
                    "purpose": "submit the same email again",
                    "expected_status_codes": [409],
                    "repeat": "once",
                    "bind": [
                        {
                            "location": "body",
                            "name": "email",
                            "variable": "created_email",
                        }
                    ],
                },
            ],
        }
    )


def test_valid_diagnostic_is_compiled_and_inserted_at_queue_front():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    decision = AnalysisDecision(
        decision="add_diagnostic_workflow",
        reason="distinguish a one-off response from missing uniqueness enforcement",
        hypothesis=AgentHypothesis(
            statement="duplicate email protection may be missing",
            confidence="medium",
            supporting_evidence=["trajectory:actions/1"],
        ),
        diagnostic_workflow=_diagnostic_workflow(),
    )

    applied = _apply(state, decision, catalog)

    assert applied.diagnostic_workflow is not None
    assert applied.record.validation_errors == []
    assert applied.record.applied_decision == "add_diagnostic_workflow"
    assert applied.state.pending_actions[0].item_id == "diagnose_duplicate_email"
    assert applied.state.phase == "executing"
    assert applied.state.plan_version == 2
    assert applied.state.remaining_step_budget == 4
    assert applied.state.hypotheses[0].confidence == "medium"


def test_single_step_diagnostic_can_bind_a_working_memory_variable():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.variables.append(
        AgentVariable(
            action_id="order-create-read",
            name="order_id",
            value=42,
            producer_operation_id="createOrder",
            producer_case_id="WF-order-create-read-create",
            response_expression="$response.body#/id",
        )
    )
    decision = AnalysisDecision.model_validate(
        {
            "decision": "add_diagnostic_case",
            "reason": "read the current order status before assigning a root cause",
            "diagnostic_case": {
                "case_id": "diagnose_order_status",
                "operation_id": "getOrder",
                "purpose": "inspect the failed order's current state",
                "expected_status_codes": [200],
                "bindings": [
                    {
                        "location": "path",
                        "name": "id",
                        "variable": "order_id",
                    }
                ],
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.validation_errors == []
    assert applied.diagnostic_case is not None
    assert applied.diagnostic_case.request.path_parameters == {"id": 42}
    assert applied.diagnostic_case.dependencies == ["WF-order-create-read-create"]
    assert applied.state.pending_actions[0].item_id == "DIAG-diagnose_order_status"
    assert applied.state.pending_actions[0].estimated_requests == 1


def test_invalid_diagnostic_falls_back_to_existing_plan():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    payload = _diagnostic_workflow().model_dump(mode="json")
    payload["steps"][1]["bind"][0]["name"] = "missing_field"
    decision = AnalysisDecision(
        decision="add_diagnostic_workflow",
        reason="try another request",
        diagnostic_workflow=WorkflowCandidate.model_validate(payload),
    )

    applied = _apply(_state(catalog), decision, catalog)

    assert applied.diagnostic_workflow is None
    assert applied.record.applied_decision == "continue_plan"
    assert "unknown request body field" in applied.record.validation_errors[0]
    assert applied.state.pending_actions[0].item_id == "TC-pending"
    assert applied.state.plan_version == 1
    assert applied.state.phase == "executing"


def test_prompt_contains_real_failure_and_remaining_budgets():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    prompt = build_analysis_prompt(_state(catalog), _failure(), catalog)

    assert prompt["triggering_action"]["observation"]["outcome"] == "failed"
    assert prompt["working_memory"]["remaining_test_budget"] == 4
    assert prompt["catalog"]["operations"]


def test_analysis_checkpoint_is_a_distinct_trajectory_event(tmp_path):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    decision = AnalysisDecision(
        decision="continue_plan",
        reason="the remaining independent test is more informative",
    )
    applied = _apply(_state(catalog), decision, catalog)
    store = AgentArtifactStore(tmp_path / "run")

    store.checkpoint_analysis(applied.state, applied.record)

    event = json.loads(store.trajectory_path.read_text(encoding="utf-8"))
    checkpoint = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
    assert event["event"] == "analysis"
    assert event["analysis"]["applied_decision"] == "continue_plan"
    assert checkpoint["state"]["remaining_step_budget"] == 4


def test_analyzer_failure_uses_auditable_fallback_without_exception_message():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")

    applied = apply_analyzer_failure(
        _state(catalog), TimeoutError("secret provider response")
    )

    assert applied.state.phase == "executing"
    assert applied.state.remaining_step_budget == 4
    assert applied.record.proposed is None
    assert applied.record.applied_decision == "continue_plan"
    assert applied.record.analyzer_error == "Analyzer unavailable (TimeoutError)"
    assert "secret" not in applied.record.analyzer_error


def test_analyzer_failure_finishes_when_no_pending_action_remains():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")

    applied = apply_analyzer_failure(
        _state(catalog, pending=False), ValueError("invalid model output")
    )

    assert applied.state.phase == "finished"
    assert applied.state.termination_reason == "no pending action after analyzer failure"
    assert applied.record.applied_decision == "finish"
