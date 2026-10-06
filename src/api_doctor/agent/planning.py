"""Ask an LLM to prioritize trusted work, then enforce deterministic plan limits."""

from __future__ import annotations

from api_doctor.agent.models import (
    AgentState,
    InitialPlanningResult,
    InitialPlanProposal,
    PlannedAction,
)
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.testspec.fusion import FinalTestPlan
from api_doctor.workflows.validation import CompiledWorkflow


def _case_context(plan: FinalTestPlan) -> list[dict[str, object]]:
    return [
        {
            "item_id": item.case.case_id,
            "operation_id": item.case.operation_id,
            "purpose": item.case.purpose,
            "method": item.case.request.method,
            "path": item.case.request.path_template,
            "risk": item.case.risk,
            "sources": sorted({source.source for source in item.case.assertions}),
            "estimated_requests": 1,
        }
        for item in plan.cases
    ]


def _workflow_context(workflows: list[CompiledWorkflow]) -> list[dict[str, object]]:
    return [
        {
            "item_id": workflow.workflow_id,
            "purpose": workflow.purpose,
            "confidence": workflow.confidence,
            "operations": [step.operation_id for step in workflow.steps],
            "requires_approval": workflow.requires_approval,
            "estimated_requests": len(workflow.steps),
        }
        for workflow in workflows
    ]


def build_initial_plan_prompt(
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    *,
    max_tests: int,
    max_steps: int,
    delete_approved: bool = False,
) -> dict[str, object]:
    executable_step_budget = max(max_steps - 1, 0)
    return {
        "task": "Select and prioritize trusted API tests for the initial agent plan. Reference only supplied item IDs; do not create requests, operations, assertions, or URLs.",
        "goal": "Maximize contract and semantic fault information within the request budget while respecting dependencies and risk.",
        "constraints": [
            f"The sum of estimated_requests must not exceed {max_tests}.",
            f"Initial planning consumes one agent step; selected request cost must not exceed {executable_step_budget} remaining agent steps.",
            "Prefer workflows that unlock validated cross-operation coverage.",
            "Balance happy paths, contract negatives, and semantic business-rule tests.",
            "DELETE workflows still require human approval; selection does not grant approval.",
            "Use only the provided item_id values and matching kind.",
        ],
        "test_cases": _case_context(plan),
        "workflows": _workflow_context(
            [item for item in workflows if delete_approved or not item.requires_approval]
        ),
        "output_json_schema": InitialPlanProposal.model_json_schema(),
    }


def propose_initial_plan(
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    *,
    max_tests: int,
    max_steps: int,
    delete_approved: bool = False,
    gateway: ModelGateway,
) -> ModelResponse[InitialPlanProposal]:
    return gateway.complete(
        build_initial_plan_prompt(
            plan,
            workflows,
            max_tests=max_tests,
            max_steps=max_steps,
            delete_approved=delete_approved,
        ),
        response_model=InitialPlanProposal,
        trace_label="initial-planner",
    )


def _inventory(
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    *,
    delete_approved: bool,
) -> dict[tuple[str, str], int]:
    inventory = {
        ("test_case", item.case.case_id): 1
        for item in plan.cases
    }
    inventory.update(
        {
            ("workflow", workflow.workflow_id): len(workflow.steps)
            for workflow in workflows
            if delete_approved or not workflow.requires_approval
        }
    )
    return inventory


def _validate_proposal(
    proposal: InitialPlanProposal,
    inventory: dict[tuple[str, str], int],
    *,
    max_tests: int,
    max_steps: int,
) -> list[str]:
    errors: list[str] = []
    request_count = 0
    for action in proposal.selected:
        key = (action.kind, action.item_id)
        actual_cost = inventory.get(key)
        if actual_cost is None:
            errors.append(f"unknown {action.kind} item_id {action.item_id!r}")
            continue
        if action.estimated_requests != actual_cost:
            errors.append(f"estimated request cost drifted for {action.item_id!r}")
        request_count += actual_cost
    if request_count > max_tests:
        errors.append("selected request cost exceeds max_tests")
    if request_count > max(max_steps - 1, 0):
        errors.append("selected request cost exceeds remaining agent step budget")
    if inventory and not proposal.selected:
        errors.append("initial plan selected no executable work")
    return errors


def _fallback_plan(
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    *,
    max_tests: int,
    max_steps: int,
    delete_approved: bool,
) -> InitialPlanProposal:
    selected: list[PlannedAction] = []
    remaining = min(max_tests, max(max_steps - 1, 0))
    for workflow in sorted(
        [item for item in workflows if delete_approved or not item.requires_approval],
        key=lambda item: (item.requires_approval, -len(item.steps), item.workflow_id),
    ):
        cost = len(workflow.steps)
        if cost <= remaining:
            selected.append(
                PlannedAction(
                    kind="workflow",
                    item_id=workflow.workflow_id,
                    priority=80,
                    reason="deterministic fallback prioritizes validated cross-operation coverage",
                    estimated_requests=cost,
                )
            )
            remaining -= cost
    for item in plan.cases:
        if remaining < 1:
            break
        selected.append(
            PlannedAction(
                kind="test_case",
                item_id=item.case.case_id,
                priority=50,
                reason="deterministic fallback preserves stable fused-plan order",
                estimated_requests=1,
            )
        )
        remaining -= 1
    return InitialPlanProposal(strategy="deterministic fallback", selected=selected)


def initialize_agent_state(
    *,
    spec_fingerprint: str,
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    proposed_plan: InitialPlanProposal,
    max_tests: int,
    max_steps: int,
    delete_approved: bool = False,
) -> InitialPlanningResult:
    if max_tests < 1:
        raise ValueError("max_tests must be at least 1")
    if max_steps < 1:
        raise ValueError("max_steps must be at least 1")
    errors = _validate_proposal(
        proposed_plan,
        _inventory(plan, workflows, delete_approved=delete_approved),
        max_tests=max_tests,
        max_steps=max_steps,
    )
    accepted = (
        _fallback_plan(
            plan,
            workflows,
            max_tests=max_tests,
            max_steps=max_steps,
            delete_approved=delete_approved,
        )
        if errors
        else proposed_plan
    )
    return InitialPlanningResult(
        proposed_plan=proposed_plan,
        used_fallback=bool(errors),
        validation_errors=errors,
        state=AgentState(
            spec_fingerprint=spec_fingerprint,
            phase="executing" if accepted.selected else "finished",
            strategy=accepted.strategy,
            pending_actions=accepted.selected,
            remaining_test_budget=max_tests,
            remaining_step_budget=max_steps - 1,
            delete_approved=delete_approved,
            termination_reason=None if accepted.selected else "no executable work",
        ),
    )
