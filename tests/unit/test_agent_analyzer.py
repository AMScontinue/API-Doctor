import json
from datetime import datetime
from pathlib import Path

import pytest

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
from api_doctor.testspec.fusion import request_fingerprint


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
        observations=[
            AgentObservation(
                action_kind="test_case",
                action_id="TC-failed",
                outcome="failed",
                evidence_ref="trajectory:actions/1",
                summary="unexpected response",
                request_count=1,
            )
        ],
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
        new_hypothesis=AgentHypothesis(
            hypothesis_id="H-duplicate-email",
            statement="duplicate email protection may be missing",
            confidence="medium",
            supporting_evidence=["trajectory:actions/1"],
        ),
        diagnostic_hypothesis_id="H-duplicate-email",
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
    assert applied.state.pending_actions[0].hypothesis_id == "H-duplicate-email"


def test_analysis_schema_rejects_a_new_hypothesis_that_skips_open_state():
    with pytest.raises(ValueError, match="must start with status 'open'"):
        AnalysisDecision.model_validate(
            {
                "decision": "finish_run",
                "reason": "stop after a direct failure",
                "new_hypothesis": {
                    "hypothesis_id": "H-direct",
                    "statement": "the observed operation violates its contract",
                    "status": "confirmed",
                    "confidence": "high",
                    "supporting_evidence": ["trajectory:actions/1"],
                },
            }
        )


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
            "new_hypothesis": {
                "hypothesis_id": "H-order-state",
                "statement": "the order may not be in the required initial state",
                "status": "open",
                "confidence": "medium",
                "supporting_evidence": ["trajectory:actions/1"],
            },
            "diagnostic_hypothesis_id": "H-order-state",
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


def test_single_step_diagnostic_can_assign_a_validated_constant():
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
            "reason": "retry the exact transition with a grounded order id",
            "new_hypothesis": {
                "hypothesis_id": "H-confirm-transition",
                "statement": "the valid confirm transition may be broken",
                "confidence": "medium",
                "supporting_evidence": ["trajectory:actions/1"],
            },
            "diagnostic_hypothesis_id": "H-confirm-transition",
            "diagnostic_case": {
                "case_id": "confirm_order_once",
                "operation_id": "updateOrderStatus",
                "purpose": "apply the confirmed state once",
                "expected_status_codes": [200],
                "bindings": [
                    {"location": "path", "name": "id", "variable": "order_id"}
                ],
                "assignments": [
                    {"location": "body", "name": "status", "value": "confirmed"}
                ],
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.validation_errors == []
    assert applied.diagnostic_case.request.path_parameters == {"id": 42}
    assert applied.diagnostic_case.request.body["status"] == "confirmed"


def test_rejects_schema_invalid_diagnostic_constant_atomically():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    decision = AnalysisDecision.model_validate(
        {
            "decision": "add_diagnostic_case",
            "reason": "try an unsupported state",
            "new_hypothesis": {
                "hypothesis_id": "H-invalid-state",
                "statement": "an unsupported state may reveal validation behavior",
                "confidence": "low",
                "supporting_evidence": ["trajectory:actions/1"],
            },
            "diagnostic_hypothesis_id": "H-invalid-state",
            "diagnostic_case": {
                "case_id": "invalid_state",
                "operation_id": "updateOrderStatus",
                "purpose": "attempt an unsupported state",
                "expected_status_codes": [200],
                "assignments": [
                    {"location": "body", "name": "status", "value": "invented"}
                ],
            },
        }
    )

    applied = _apply(_state(catalog), decision, catalog)

    assert applied.record.applied_decision == "continue_plan"
    assert "violates target schema" in applied.record.validation_errors[0]
    assert applied.state.hypotheses == []


def test_allows_one_evidence_backed_single_request_diagnostic_repeat():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.variables.append(
        AgentVariable(
            action_id="create-order",
            name="order_id",
            value=42,
            producer_operation_id="createOrder",
            producer_case_id="WF-create-order",
            response_expression="$response.body#/id",
        )
    )
    state.hypotheses.append(
        AgentHypothesis(
            hypothesis_id="H-transition",
            statement="the transition fails consistently",
            confidence="medium",
            supporting_evidence=["trajectory:actions/1"],
        )
    )
    payload = {
        "decision": "add_diagnostic_case",
        "reason": "replay once to distinguish a transient response",
        "diagnostic_hypothesis_id": "H-transition",
        "diagnostic_case": {
            "case_id": "repeat_confirm",
            "operation_id": "updateOrderStatus",
            "purpose": "repeat the observed transition exactly once",
            "expected_status_codes": [200, 409],
            "repeat": "once",
            "bindings": [
                {"location": "path", "name": "id", "variable": "order_id"}
            ],
            "assignments": [
                {"location": "body", "name": "status", "value": "confirmed"}
            ],
        },
    }
    proposal = AnalysisDecision.model_validate(payload)
    compiled = _apply(
        state,
        AnalysisDecision.model_validate(
            {
                **payload,
                "diagnostic_case": {**payload["diagnostic_case"], "repeat": "forbid"},
            }
        ),
        catalog,
    ).diagnostic_case
    state.executed_fingerprints.add(request_fingerprint(compiled))

    applied = _apply(state, proposal, catalog)

    assert applied.record.validation_errors == []
    assert applied.record.added_action.allow_repeat is True
    assert applied.diagnostic_case is not None


def test_invalid_diagnostic_falls_back_to_existing_plan():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    payload = _diagnostic_workflow().model_dump(mode="json")
    payload["steps"][1]["bind"][0]["name"] = "missing_field"
    decision = AnalysisDecision(
        decision="add_diagnostic_workflow",
        reason="try another request",
        new_hypothesis=AgentHypothesis(
            hypothesis_id="H-invalid-proposal",
            statement="another request may distinguish the failure",
            confidence="low",
            supporting_evidence=["trajectory:actions/1"],
        ),
        diagnostic_hypothesis_id="H-invalid-proposal",
        diagnostic_workflow=WorkflowCandidate.model_validate(payload),
    )

    applied = _apply(_state(catalog), decision, catalog)

    assert applied.diagnostic_workflow is None
    assert applied.record.applied_decision == "continue_plan"
    assert "unknown request body field" in applied.record.validation_errors[0]
    assert applied.state.pending_actions[0].item_id == "TC-pending"
    assert applied.state.plan_version == 1
    assert applied.state.phase == "executing"
    assert applied.state.hypotheses == []


def test_prompt_contains_real_failure_and_remaining_budgets():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    prompt = build_analysis_prompt(_state(catalog), _failure(), catalog)

    assert prompt["triggering_action"]["observation"]["outcome"] == "failed"
    assert prompt["working_memory"]["remaining_test_budget"] == 4
    assert prompt["budget_outlook"] == {
        "current_analysis_step_cost": 1,
        "execution_steps_after_current_analysis": 4,
        "diagnostic_followup_analysis_step_cost": 1,
        "max_diagnostic_request_cost_with_followup": 3,
        "pending_action_count": 1,
        "pending_trusted_request_cost": 1,
        "pending_fits_request_budget": True,
        "pending_fits_execution_step_budget": True,
    }
    assert prompt["evidence_registry"][0]["evidence_ref"] == "trajectory:actions/1"
    assert prompt["catalog"]["operations"]


def test_rejects_diagnostic_that_cannot_leave_a_followup_analysis_step():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.remaining_step_budget = 2
    decision = AnalysisDecision.model_validate(
        {
            "decision": "add_diagnostic_case",
            "reason": "probe the resource once",
            "new_hypothesis": {
                "hypothesis_id": "H-resource",
                "statement": "the resource may not exist",
                "confidence": "medium",
                "supporting_evidence": ["trajectory:actions/1"],
            },
            "diagnostic_hypothesis_id": "H-resource",
            "diagnostic_case": {
                "case_id": "probe_resource",
                "operation_id": "getUser",
                "purpose": "inspect the resource",
                "expected_status_codes": [404],
                "assignments": [
                    {"location": "path", "name": "id", "value": 999999}
                ],
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.applied_decision == "continue_plan"
    assert applied.record.validation_errors == [
        "diagnostic case cannot reserve one follow-up analysis step"
    ]
    assert applied.diagnostic_case is None
    assert applied.state.hypotheses == []
    assert applied.state.remaining_step_budget == 1


def test_prompt_assigns_distinct_memory_keys_to_same_named_variables():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.variables.extend(
        [
            AgentVariable(
                action_id=f"workflow-{index}",
                name="order_id",
                value=index,
                producer_operation_id="createOrder",
                producer_case_id=f"WF-{index}",
                response_expression="$response.body#/id",
            )
            for index in (1, 2)
        ]
    )

    prompt = build_analysis_prompt(state, _failure(), catalog)

    variables = prompt["working_memory"]["available_variables"]
    assert [item["memory_key"] for item in variables] == [
        "memory_1_order_id",
        "memory_2_order_id",
    ]
    assert [item["value"] for item in variables] == [1, 2]


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
    assert applied.record.applied_decision == "finish_run"


def test_rejects_update_for_unknown_hypothesis():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    decision = AnalysisDecision.model_validate(
        {
            "decision": "continue_plan",
            "reason": "apply the diagnostic evidence",
            "hypothesis_update": {
                "hypothesis_id": "H-missing",
                "status": "confirmed",
                "confidence": "high",
                "supporting_evidence": ["trajectory:actions/2"],
                "rationale": "the probe reproduced the behavior",
            },
        }
    )

    applied = _apply(_state(catalog), decision, catalog)

    assert applied.record.applied_decision == "continue_plan"
    assert applied.record.validation_errors == [
        "unknown hypothesis_id 'H-missing'"
    ]
    assert applied.state.hypotheses == []


def test_finish_investigation_closes_hypothesis_and_resumes_pending_plan():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.hypotheses.append(
        AgentHypothesis(
            hypothesis_id="H-order-state",
            statement="the order state caused the conflict",
            confidence="medium",
        )
    )
    decision = AnalysisDecision.model_validate(
        {
            "decision": "finish_investigation",
            "reason": "the observation rules out the state precondition",
            "hypothesis_update": {
                "hypothesis_id": "H-order-state",
                "status": "rejected",
                "confidence": "high",
                "contradicting_evidence": ["trajectory:actions/1"],
                "rationale": "the current state was valid",
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.applied_decision == "finish_investigation"
    assert applied.state.hypotheses[0].status == "rejected"
    assert applied.state.phase == "executing"
    assert applied.state.pending_actions[0].item_id == "TC-pending"


def test_finish_run_with_pending_work_requires_confirmed_high_confidence_evidence():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.hypotheses.append(
        AgentHypothesis(
            hypothesis_id="H-systemic",
            statement="the service violates a shared invariant",
            confidence="medium",
        )
    )
    decision = AnalysisDecision.model_validate(
        {
            "decision": "finish_run",
            "reason": "continuing would repeat a confirmed systemic failure",
            "hypothesis_update": {
                "hypothesis_id": "H-systemic",
                "status": "confirmed",
                "confidence": "high",
                "supporting_evidence": ["trajectory:actions/1"],
                "rationale": "the diagnostic confirmed the shared failure",
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.validation_errors == []
    assert applied.state.phase == "finished"
    assert applied.state.hypotheses[0].status == "confirmed"
    assert applied.state.pending_actions
    assert applied.state.termination_reason.startswith("Analyzer:")


def test_rejects_weak_early_finish_without_mutating_hypothesis():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.hypotheses.append(
        AgentHypothesis(
            hypothesis_id="H-systemic",
            statement="the service may violate a shared invariant",
            confidence="low",
        )
    )
    decision = AnalysisDecision.model_validate(
        {
            "decision": "finish_run",
            "reason": "stop early",
            "hypothesis_update": {
                "hypothesis_id": "H-systemic",
                "status": "open",
                "confidence": "medium",
                "supporting_evidence": ["trajectory:actions/1"],
                "rationale": "one observation is inconclusive",
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.applied_decision == "continue_plan"
    assert "high-confidence confirmed" in applied.record.validation_errors[0]
    assert applied.state.phase == "executing"
    assert applied.state.hypotheses[0].confidence == "low"


def test_revises_pending_queue_using_only_existing_trusted_actions():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.pending_actions.extend(
        [
            PlannedAction(
                kind="workflow",
                item_id="WF-later",
                priority=40,
                reason="dependent coverage",
                estimated_requests=2,
            ),
            PlannedAction(
                kind="test_case",
                item_id="TC-redundant",
                priority=20,
                reason="now redundant",
                estimated_requests=1,
            ),
        ]
    )
    decision = AnalysisDecision.model_validate(
        {
            "decision": "revise_pending_actions",
            "reason": "run the dependent workflow first and drop redundant coverage",
            "pending_revision": {
                "actions": [
                    {"kind": "workflow", "item_id": "WF-later"},
                    {"kind": "test_case", "item_id": "TC-pending"},
                ],
                "rationale": "the failure increased the workflow's information value",
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.validation_errors == []
    assert [item.item_id for item in applied.state.pending_actions] == [
        "WF-later",
        "TC-pending",
    ]
    assert [item.item_id for item in applied.record.removed_actions] == ["TC-redundant"]
    assert applied.state.plan_version == 2


def test_pending_revision_preserves_model_order_but_omits_over_budget_actions():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    state.remaining_test_budget = 1
    state.remaining_step_budget = 3
    state.pending_actions.insert(
        0,
        PlannedAction(
            kind="workflow",
            item_id="WF-expensive",
            priority=90,
            reason="high-value lifecycle",
            estimated_requests=2,
        ),
    )
    decision = AnalysisDecision.model_validate(
        {
            "decision": "revise_pending_actions",
            "reason": "retain the most valuable work that still fits",
            "pending_revision": {
                "actions": [
                    {"kind": "workflow", "item_id": "WF-expensive"},
                    {"kind": "test_case", "item_id": "TC-pending"},
                ],
                "rationale": "prefer the lifecycle, then fall back to the compact probe",
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.validation_errors == []
    assert applied.record.adjustments == [
        "omitted over-budget pending action(s): ['WF-expensive']"
    ]
    assert [item.item_id for item in applied.state.pending_actions] == ["TC-pending"]
    assert [item.item_id for item in applied.record.removed_actions] == [
        "WF-expensive"
    ]


def test_rejects_pending_revision_that_invents_an_action():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    state = _state(catalog)
    decision = AnalysisDecision.model_validate(
        {
            "decision": "revise_pending_actions",
            "reason": "replace the remaining plan",
            "pending_revision": {
                "actions": [{"kind": "test_case", "item_id": "TC-invented"}],
                "rationale": "attempt to add an untrusted action",
            },
        }
    )

    applied = _apply(state, decision, catalog)

    assert applied.record.applied_decision == "continue_plan"
    assert "unknown action" in applied.record.validation_errors[0]
    assert applied.state.pending_actions == state.pending_actions
    assert applied.state.plan_version == 1
