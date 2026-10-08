"""Ask an LLM to prioritize trusted work, then enforce deterministic plan limits."""

from __future__ import annotations

from dataclasses import dataclass

from api_doctor.agent.models import (
    AgentState,
    InitialPlanningResult,
    InitialPlanProposal,
    PlannedAction,
)
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.testspec.fusion import FinalTestPlan, PlannedCase
from api_doctor.workflows.validation import CompiledWorkflow


@dataclass(frozen=True)
class _Coverage:
    category: str
    operations: frozenset[str]


COVERAGE_CATEGORIES = (
    "semantic",
    "stateful_workflow",
    "workflow",
    "heuristic",
    "format",
    "contract_negative",
)


def _available_cases(
    plan: FinalTestPlan,
    delete_approved: bool,
) -> list[PlannedCase]:
    return [
        item
        for item in plan.cases
        if delete_approved or item.case.risk != "requires_approval"
    ]


def _case_context(
    plan: FinalTestPlan,
    delete_approved: bool,
) -> list[dict[str, object]]:
    return [
        {
            "item_id": item.case.case_id,
            "operation_id": item.case.operation_id,
            "rule_id": item.case.rule_id,
            "purpose": item.case.purpose,
            "method": item.case.request.method,
            "path": item.case.request.path_template,
            "risk": item.case.risk,
            "sources": sorted({source.source for source in item.case.assertions}),
            "expected_status_codes": sorted(
                {code for assertion in item.case.assertions for code in assertion.allowed}
            ),
            "estimated_requests": 1,
        }
        for item in _available_cases(plan, delete_approved)
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
            "Return selected actions in descending execution priority.",
            f"The request budget is {max_tests}; the host recalculates trusted costs and keeps the highest-ranked actions that fit.",
            f"Initial planning consumes one agent step, leaving at most {executable_step_budget} execution steps.",
            "Prefer workflows that unlock validated cross-operation coverage.",
            "Balance happy paths, contract negatives, and semantic business-rule tests.",
            "Return a complete priority ranking of the supplied executable items; the host, not the model, packs the ranked prefix into the hard budgets.",
            "Within the affordable prefix, cover distinct operations and response branches before selecting many similar boundary variants for one operation.",
            "Give high information value to format validation, declared not-found/conflict branches, and evidence-backed business rules.",
            "DELETE actions require human approval; selection does not grant approval.",
            "Use only the provided item_id values and matching kind.",
        ],
        "test_cases": _case_context(plan, delete_approved),
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
        for item in _available_cases(plan, delete_approved)
    }
    inventory.update(
        {
            ("workflow", workflow.workflow_id): len(workflow.steps)
            for workflow in workflows
            if delete_approved or not workflow.requires_approval
        }
    )
    return inventory


def _coverage_inventory(
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    *,
    delete_approved: bool,
) -> dict[tuple[str, str], _Coverage]:
    coverage: dict[tuple[str, str], _Coverage] = {}
    for item in _available_cases(plan, delete_approved):
        sources = {assertion.source for assertion in item.case.assertions}
        statuses = {
            code for assertion in item.case.assertions for code in assertion.allowed
        }
        if sources & {"business_rule", "openapi_description"}:
            category = "semantic"
        elif "heuristic" in sources:
            category = "heuristic"
        elif "format" in item.case.rule_id or "format" in item.case.purpose.lower():
            category = "format"
        elif any(code >= 400 for code in statuses):
            category = "contract_negative"
        else:
            category = "success"
        coverage[("test_case", item.case.case_id)] = _Coverage(
            category=category,
            operations=frozenset({item.case.operation_id}),
        )
    for workflow in workflows:
        if not delete_approved and workflow.requires_approval:
            continue
        stateful = any(
            step.repeat == "once" or (index > 0 and step.method in {"PUT", "PATCH", "DELETE"})
            for index, step in enumerate(workflow.steps)
        )
        coverage[("workflow", workflow.workflow_id)] = _Coverage(
            category="stateful_workflow" if stateful else "workflow",
            operations=frozenset(step.operation_id for step in workflow.steps),
        )
    return coverage


def _coverage_aware_order(
    actions: list[PlannedAction],
    coverage: dict[tuple[str, str], _Coverage],
    *,
    budget: int,
) -> tuple[list[PlannedAction], list[str]]:
    """Reserve most of the budget for broad coverage, then preserve model ranking."""
    floor_budget = min(budget, max(2, (3 * budget + 3) // 4))
    anchors: list[PlannedAction] = []
    anchor_ids: set[tuple[str, str]] = set()
    covered_dimensions: set[tuple[str, str]] = set()
    spent = 0

    def add(action: PlannedAction) -> bool:
        nonlocal spent
        identity = (action.kind, action.item_id)
        if identity in anchor_ids or spent + action.estimated_requests > budget:
            return False
        anchors.append(action)
        anchor_ids.add(identity)
        spent += action.estimated_requests
        covered_dimensions.update(
            (operation, coverage[identity].category)
            for operation in coverage[identity].operations
        )
        return True

    for category in COVERAGE_CATEGORIES:
        if spent >= floor_budget:
            break
        action = next(
            (
                item
                for item in actions
                if coverage[(item.kind, item.item_id)].category == category
                and spent + item.estimated_requests <= budget
            ),
            None,
        )
        if action is not None:
            add(action)

    while spent < floor_budget:
        action = next(
            (
                item
                for item in actions
                if any(
                    (operation, coverage[(item.kind, item.item_id)].category)
                    not in covered_dimensions
                    for operation in coverage[(item.kind, item.item_id)].operations
                )
                and spent + item.estimated_requests <= budget
            ),
            None,
        )
        if action is None:
            break
        add(action)

    ordered = anchors + [
        action for action in actions if (action.kind, action.item_id) not in anchor_ids
    ]
    original_positions = {
        (action.kind, action.item_id): index for index, action in enumerate(actions)
    }
    adjustments = [
        (
            f"promoted {action.item_id!r} to preserve "
            f"{coverage[(action.kind, action.item_id)].category} coverage"
        )
        for index, action in enumerate(anchors)
        if original_positions[(action.kind, action.item_id)] != index
    ]
    return ordered, adjustments


def _prepare_proposal(
    proposal: InitialPlanProposal,
    inventory: dict[tuple[str, str], int],
    coverage: dict[tuple[str, str], _Coverage],
    *,
    max_tests: int,
    max_steps: int,
) -> tuple[InitialPlanProposal, list[str], list[str]]:
    errors: list[str] = []
    adjustments: list[str] = []
    known_actions: list[PlannedAction] = []
    for action in proposal.selected:
        key = (action.kind, action.item_id)
        actual_cost = inventory.get(key)
        if actual_cost is None:
            errors.append(f"unknown {action.kind} item_id {action.item_id!r}")
            continue
        if action.estimated_requests != actual_cost:
            adjustments.append(
                f"recalculated {action.item_id!r} request cost "
                f"from {action.estimated_requests} to {actual_cost}"
            )
        known_actions.append(action.model_copy(update={"estimated_requests": actual_cost}))
    if inventory and not proposal.selected:
        errors.append("initial plan selected no executable work")

    if errors:
        return proposal, errors, adjustments

    ranked = {(action.kind, action.item_id) for action in known_actions}
    for (kind, item_id), actual_cost in inventory.items():
        if (kind, item_id) in ranked:
            continue
        known_actions.append(
            PlannedAction(
                kind=kind,
                item_id=item_id,
                priority=0,
                reason="deterministic coverage floor completed an omitted trusted ranking",
                estimated_requests=actual_cost,
            )
        )
        adjustments.append(
            f"appended omitted trusted {kind} {item_id!r} at the end of the ranking"
        )

    remaining = min(max_tests, max(max_steps - 1, 0))
    known_actions, coverage_adjustments = _coverage_aware_order(
        known_actions,
        coverage,
        budget=remaining,
    )
    adjustments.extend(coverage_adjustments)
    selected: list[PlannedAction] = []
    for action in known_actions:
        if action.estimated_requests <= remaining:
            selected.append(action)
            remaining -= action.estimated_requests
        else:
            adjustments.append(
                f"omitted {action.item_id!r}: trusted cost "
                f"{action.estimated_requests} exceeds remaining execution budget {remaining}"
            )
    return proposal.model_copy(update={"selected": selected}), errors, adjustments


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
    for item in _available_cases(plan, delete_approved):
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
    prepared, errors, adjustments = _prepare_proposal(
        proposed_plan,
        _inventory(plan, workflows, delete_approved=delete_approved),
        _coverage_inventory(plan, workflows, delete_approved=delete_approved),
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
        else prepared
    )
    return InitialPlanningResult(
        proposed_plan=proposed_plan,
        used_fallback=bool(errors),
        validation_errors=errors,
        adjustments=adjustments,
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
