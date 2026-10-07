"""Turn a failed HTTP observation into one validated, bounded next decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from api_doctor.agent.diagnostics import DiagnosticCompileError, compile_diagnostic_case
from api_doctor.agent.models import (
    AnalysisDecision,
    AnalysisRecord,
    AgentActionRecord,
    AgentState,
    PlannedAction,
)
from api_doctor.generation.workflow_discovery import build_catalog_context
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.fusion import request_fingerprint
from api_doctor.testspec.models import CompiledTestCase
from api_doctor.workflows.validation import CompiledWorkflow, validate_workflows


@dataclass(frozen=True)
class AppliedAnalysis:
    state: AgentState
    record: AnalysisRecord
    diagnostic_case: CompiledTestCase | None = None
    diagnostic_workflow: CompiledWorkflow | None = None


def build_analysis_prompt(
    state: AgentState,
    trigger: AgentActionRecord,
    catalog: OperationCatalog,
) -> dict[str, object]:
    """Expose evidence and capabilities, not authority to execute requests."""
    return {
        "task": "Analyze the latest API observation and choose one bounded next action.",
        "constraints": [
            "Base every hypothesis on the supplied observation and execution evidence.",
            "Choose continue_plan when a diagnostic would not distinguish plausible causes.",
            "Use add_diagnostic_case for exactly one HTTP request.",
            "Use add_diagnostic_workflow only for a two-to-four-step dependent experiment.",
            "Use only catalog operation_id values, declared fields, and declared status codes.",
            "Do not request DELETE unless it is essential; host approval is still required.",
            "Do not repeat an earlier request unless repeat=once is evidence-backed.",
            "A diagnostic case binding always reads a named working-memory variable; never invent its value.",
            "A diagnostic workflow uses binding source=working_memory only for existing state, or source=workflow for an earlier extraction.",
            "The host validates every proposal and controls budgets and HTTP execution.",
        ],
        "triggering_action": trigger.model_dump(mode="json"),
        "working_memory": {
            "variables": [item.model_dump(mode="json") for item in state.variables],
            "hypotheses": [item.model_dump(mode="json") for item in state.hypotheses],
            "executed_fingerprints": sorted(state.executed_fingerprints),
            "remaining_test_budget": state.remaining_test_budget,
            "remaining_step_budget": state.remaining_step_budget,
            "pending_actions": [item.model_dump(mode="json") for item in state.pending_actions],
            "delete_approved": state.delete_approved,
        },
        "catalog": build_catalog_context(catalog),
        "output_json_schema": AnalysisDecision.model_json_schema(),
    }


def analyze_observation(
    state: AgentState,
    trigger: AgentActionRecord,
    catalog: OperationCatalog,
    gateway: ModelGateway,
) -> ModelResponse[AnalysisDecision]:
    return gateway.complete(
        build_analysis_prompt(state, trigger, catalog),
        response_model=AnalysisDecision,
        trace_label="observation-analyzer",
    )


def apply_analyzer_failure(state: AgentState, error: Exception) -> AppliedAnalysis:
    """Apply a deterministic fallback without persisting unsafe exception content."""
    updated = state.model_copy(deep=True)
    updated.remaining_step_budget -= 1
    error_summary = f"Analyzer unavailable ({type(error).__name__})"
    if updated.remaining_step_budget == 0:
        applied = "finish"
        updated.phase = "finished"
        updated.termination_reason = "agent step budget exhausted after analyzer failure"
    elif updated.pending_actions:
        applied = "continue_plan"
        updated.phase = "executing"
        updated.termination_reason = None
    else:
        applied = "finish"
        updated.phase = "finished"
        updated.termination_reason = "no pending action after analyzer failure"
    return AppliedAnalysis(
        state=updated,
        record=AnalysisRecord(
            applied_decision=applied,
            analyzer_error=error_summary,
        ),
    )


def apply_analysis_decision(
    state: AgentState,
    decision: AnalysisDecision,
    catalog: OperationCatalog,
    *,
    seed: int,
    anchor: datetime,
    existing_case_ids: set[str] | None = None,
    existing_workflow_ids: set[str] | None = None,
) -> AppliedAnalysis:
    """Validate an untrusted decision and return an updated copy of working state."""
    updated = state.model_copy(deep=True)
    updated.remaining_step_budget -= 1
    if decision.hypothesis is not None:
        updated.hypotheses.append(decision.hypothesis)

    errors: list[str] = []
    compiled_case: CompiledTestCase | None = None
    compiled_workflow: CompiledWorkflow | None = None
    added_action: PlannedAction | None = None
    applied = decision.decision

    working_variables = updated.unambiguous_variables()
    if decision.decision == "add_diagnostic_case":
        try:
            compiled_case = compile_diagnostic_case(
                decision.diagnostic_case,
                catalog,
                working_variables,
                seed=seed,
                anchor=anchor,
            )
        except (DiagnosticCompileError, ValueError) as error:
            errors.append(str(error))
        if compiled_case is not None and compiled_case.risk == "requires_approval":
            if not updated.delete_approved:
                errors.append("diagnostic case requires ungranted DELETE approval")
        known_case_ids = existing_case_ids or set()
        planned_case_ids = {
            item.item_id
            for item in [*updated.pending_actions, *updated.completed_actions]
            if item.kind == "test_case"
        }
        if compiled_case is not None and compiled_case.case_id in known_case_ids | planned_case_ids:
            errors.append("diagnostic case_id already exists in the Agent plan")
        if (
            compiled_case is not None
            and request_fingerprint(compiled_case) in updated.executed_fingerprints
        ):
            errors.append("diagnostic case duplicates an executed request")
        if compiled_case is not None and updated.remaining_test_budget < 1:
            errors.append("diagnostic case exceeds remaining request budget")
        if compiled_case is not None and updated.remaining_step_budget < 1:
            errors.append("diagnostic case exceeds remaining agent step budget")

        if errors:
            compiled_case = None
            applied = "continue_plan"
        else:
            added_action = PlannedAction(
                kind="test_case",
                item_id=compiled_case.case_id,
                priority=100,
                reason=decision.reason,
                estimated_requests=1,
                origin="diagnostic",
            )
            updated.pending_actions.insert(0, added_action)
            updated.plan_version += 1

    elif decision.decision == "add_diagnostic_workflow":
        review = validate_workflows(
            [decision.diagnostic_workflow],
            catalog,
            working_memory={name: item.value for name, item in working_variables.items()},
        ).reviews[0]
        errors.extend(review.errors)
        compiled_workflow = review.compiled_workflow
        if (
            compiled_workflow is not None
            and compiled_workflow.requires_approval
            and not updated.delete_approved
        ):
            errors.append("diagnostic workflow requires ungranted DELETE approval")
        cost = len(compiled_workflow.steps) if compiled_workflow is not None else 0
        if compiled_workflow is not None and cost > updated.remaining_test_budget:
            errors.append("diagnostic workflow exceeds remaining request budget")
        if compiled_workflow is not None and cost > updated.remaining_step_budget:
            errors.append("diagnostic workflow exceeds remaining agent step budget")
        known_ids = existing_workflow_ids or set()
        planned_ids = {
            item.item_id
            for item in [*updated.pending_actions, *updated.completed_actions]
            if item.kind == "workflow"
        }
        if (
            compiled_workflow is not None
            and compiled_workflow.workflow_id in known_ids | planned_ids
        ):
            errors.append("diagnostic workflow_id already exists in the Agent plan")

        if errors:
            compiled_workflow = None
            applied = "continue_plan"
        else:
            added_action = PlannedAction(
                kind="workflow",
                item_id=compiled_workflow.workflow_id,
                priority=100,
                reason=decision.reason,
                estimated_requests=cost,
                origin="diagnostic",
            )
            updated.pending_actions.insert(0, added_action)
            updated.plan_version += 1

    if applied == "finish":
        updated.phase = "finished"
        updated.termination_reason = f"Analyzer: {decision.reason}"
    elif updated.remaining_step_budget == 0:
        updated.phase = "finished"
        updated.termination_reason = "agent step budget exhausted after analysis"
    elif updated.pending_actions:
        updated.phase = "executing"
        updated.termination_reason = None
    else:
        updated.phase = "finished"
        updated.termination_reason = "no pending action after analysis"

    return AppliedAnalysis(
        state=updated,
        record=AnalysisRecord(
            proposed=decision,
            applied_decision=applied,
            validation_errors=errors,
            added_action=added_action,
        ),
        diagnostic_case=compiled_case,
        diagnostic_workflow=compiled_workflow,
    )
