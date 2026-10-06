from datetime import datetime
from pathlib import Path

from api_doctor.agent.models import InitialPlanProposal
from api_doctor.agent.planning import (
    build_initial_plan_prompt,
    initialize_agent_state,
    propose_initial_plan,
)
from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.generation.workflow_models import WorkflowCandidate
from api_doctor.llm.gateway import ModelCallTelemetry, ModelResponse
from api_doctor.openapi import load_openapi
from api_doctor.testspec.fusion import fuse_test_cases
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate
from api_doctor.workflows.validation import validate_workflows


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


class FakeGateway:
    def __init__(self, proposal: InitialPlanProposal) -> None:
        self.proposal = proposal
        self.prompt = None

    def complete(self, prompt, *, response_model, trace_label):
        self.prompt = prompt
        assert response_model is InitialPlanProposal
        assert trace_label == "initial-planner"
        return ModelResponse(
            value=self.proposal,
            telemetry=ModelCallTelemetry(calls=1, duration_ms=2),
        )


def _inputs():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    candidates = [
        candidate
        for candidate in generate_schema_candidates(catalog)
        if candidate.rule_id
        in {"schema:createUser:happy-path", "schema:getUser:path-id-not-found"}
    ]
    cases = [
        compile_openapi_candidate(
            candidate,
            catalog,
            case_number=index,
            seed=7,
            anchor=ANCHOR,
        )
        for index, candidate in enumerate(candidates, start=1)
    ]
    plan = fuse_test_cases(cases)
    workflow_candidate = WorkflowCandidate.model_validate(
        {
            "workflow_id": "create_read_user",
            "purpose": "create then read one user",
            "confidence": "high",
            "reasoning": "create response id matches read path id",
            "steps": [
                {
                    "step_id": "create",
                    "operation_id": "createUser",
                    "purpose": "create",
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
                    "purpose": "read",
                    "bind": [{"location": "path", "name": "id", "variable": "user_id"}],
                },
            ],
        }
    )
    workflow = validate_workflows([workflow_candidate], catalog).reviews[0].compiled_workflow
    assert workflow is not None
    return catalog, plan, [workflow]


def _proposal(**action_overrides) -> InitialPlanProposal:
    action = {
        "kind": "workflow",
        "item_id": "create_read_user",
        "priority": 90,
        "reason": "covers a validated dependency with two operations",
        "estimated_requests": 2,
    }
    action.update(action_overrides)
    return InitialPlanProposal(strategy="exercise the lifecycle first", selected=[action])


def test_planner_receives_only_compact_trusted_inventory():
    _, plan, workflows = _inputs()
    gateway = FakeGateway(_proposal())

    response = propose_initial_plan(
        plan,
        workflows,
        max_tests=3,
        max_steps=4,
        gateway=gateway,
    )

    assert response.value.selected[0].item_id == "create_read_user"
    assert gateway.prompt == build_initial_plan_prompt(
        plan, workflows, max_tests=3, max_steps=4
    )
    assert "request" not in gateway.prompt["test_cases"][0]


def test_valid_model_plan_initializes_explicit_working_state():
    catalog, plan, workflows = _inputs()
    result = initialize_agent_state(
        spec_fingerprint=catalog.fingerprint,
        plan=plan,
        workflows=workflows,
        proposed_plan=_proposal(),
        max_tests=3,
        max_steps=4,
    )

    assert result.used_fallback is False
    assert result.state.phase == "executing"
    assert result.state.plan_version == 1
    assert result.state.pending_actions[0].item_id == "create_read_user"
    assert result.state.remaining_test_budget == 3
    assert result.state.remaining_step_budget == 3
    assert result.state.observations == []


def test_unknown_model_reference_triggers_deterministic_fallback():
    catalog, plan, workflows = _inputs()
    result = initialize_agent_state(
        spec_fingerprint=catalog.fingerprint,
        plan=plan,
        workflows=workflows,
        proposed_plan=_proposal(item_id="invented_workflow"),
        max_tests=3,
        max_steps=4,
    )

    assert result.used_fallback is True
    assert "unknown workflow item_id" in result.validation_errors[0]
    assert result.state.strategy == "deterministic fallback"
    assert result.state.pending_actions[0].item_id == "create_read_user"


def test_cost_drift_and_budget_overflow_cannot_enter_agent_state():
    catalog, plan, workflows = _inputs()
    proposal = InitialPlanProposal(
        strategy="claim cheap coverage",
        selected=[
            _proposal(estimated_requests=1).selected[0],
            {
                "kind": "test_case",
                "item_id": plan.cases[0].case.case_id,
                "priority": 80,
                "reason": "additional test",
                "estimated_requests": 1,
            },
        ],
    )
    result = initialize_agent_state(
        spec_fingerprint=catalog.fingerprint,
        plan=plan,
        workflows=workflows,
        proposed_plan=proposal,
        max_tests=2,
        max_steps=4,
    )

    assert result.used_fallback is True
    assert any("cost drifted" in error for error in result.validation_errors)
    assert any("exceeds max_tests" in error for error in result.validation_errors)
    assert sum(item.estimated_requests for item in result.state.pending_actions) <= 2


def test_unapproved_delete_workflow_is_absent_from_inventory_and_fallback():
    catalog, plan, workflows = _inputs()
    dangerous = workflows[0].model_copy(
        deep=True,
        update={"workflow_id": "dangerous_cleanup", "requires_approval": True},
    )
    proposal = _proposal(item_id="dangerous_cleanup")
    result = initialize_agent_state(
        spec_fingerprint=catalog.fingerprint,
        plan=plan,
        workflows=[dangerous, *workflows],
        proposed_plan=proposal,
        max_tests=3,
        max_steps=4,
        delete_approved=False,
    )

    assert result.used_fallback is True
    assert all(item.item_id != "dangerous_cleanup" for item in result.state.pending_actions)
    prompt = build_initial_plan_prompt(
        plan,
        [dangerous, *workflows],
        max_tests=3,
        max_steps=4,
        delete_approved=False,
    )
    assert {item["item_id"] for item in prompt["workflows"]} == {"create_read_user"}


def test_unapproved_delete_test_case_is_absent_from_prompt_and_fallback():
    catalog, plan, workflows = _inputs()
    delete_case = plan.cases[0].model_copy(
        deep=True,
        update={
            "case": plan.cases[0].case.model_copy(
                update={
                    "case_id": "delete-case",
                    "risk": "requires_approval",
                    "request": plan.cases[0].case.request.model_copy(
                        update={"method": "DELETE"}
                    ),
                }
            )
        },
    )
    dangerous_plan = plan.model_copy(update={"cases": [delete_case]})

    prompt = build_initial_plan_prompt(
        dangerous_plan,
        workflows,
        max_tests=3,
        max_steps=4,
        delete_approved=False,
    )
    result = initialize_agent_state(
        spec_fingerprint=catalog.fingerprint,
        plan=dangerous_plan,
        workflows=[],
        proposed_plan=InitialPlanProposal(strategy="unsafe", selected=[]),
        max_tests=3,
        max_steps=4,
        delete_approved=False,
    )

    assert prompt["test_cases"] == []
    assert result.state.pending_actions == []
