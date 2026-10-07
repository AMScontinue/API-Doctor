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
    available_variables = state.unambiguous_variables()
    return {
        "task": "Analyze the latest API observation and choose one bounded next action.",
        "constraints": [
            "Base every hypothesis on the supplied observation and execution evidence.",
            "In hypothesis evidence arrays, use only exact evidence_ref strings from evidence_registry; do not write prose evidence there.",
            "Create a stable new_hypothesis with status=open when a new explanation needs testing.",
            "After a diagnostic, use hypothesis_update to keep, confirm, or reject its linked hypothesis; do not restate it as a new hypothesis.",
            "Every diagnostic decision must reference an open hypothesis using diagnostic_hypothesis_id.",
            "Choose continue_plan when a diagnostic would not distinguish plausible causes.",
            "Use add_diagnostic_case for exactly one HTTP request.",
            "Use add_diagnostic_workflow only for a two-to-four-step dependent experiment.",
            "Use finish_investigation only after confirming or rejecting the active hypothesis; remaining planned tests then continue.",
            "Use revise_pending_actions only to retain and reorder a subset of the supplied pending actions; it cannot invent actions.",
            "Use finish_run with pending work only after a high-confidence confirmed hypothesis makes further testing unnecessary or unsafe.",
            "Use only catalog operation_id values, declared fields, and declared status codes.",
            "Do not request DELETE unless it is essential; host approval is still required.",
            "Do not repeat an earlier request unless repeat=once is evidence-backed.",
            "For a single-request diagnostic, repeat=once permits exactly one intentional replay of an executed request fingerprint; otherwise leave repeat=forbid.",
            "A diagnostic binding variable must copy an exact memory_key from available_variables; never use the display name or invent a value.",
            "A diagnostic case may use assignments for schema-valid constants required by one request; never bind and assign the same target.",
            "A diagnostic workflow uses binding source=working_memory only for existing state, or source=workflow for an earlier extraction.",
            "A diagnostic workflow may use assign for a schema-valid constant required by the experiment; never bind and assign the same target.",
            "The host validates every proposal and controls budgets and HTTP execution.",
        ],
        "triggering_action": trigger.model_dump(mode="json"),
        "working_memory": {
            "available_variables": [
                {"memory_key": key, **item.model_dump(mode="json")}
                for key, item in available_variables.items()
            ],
            "hypotheses": [item.model_dump(mode="json") for item in state.hypotheses],
            "executed_fingerprints": sorted(state.executed_fingerprints),
            "remaining_test_budget": state.remaining_test_budget,
            "remaining_step_budget": state.remaining_step_budget,
            "pending_actions": [item.model_dump(mode="json") for item in state.pending_actions],
            "delete_approved": state.delete_approved,
        },
        "evidence_registry": [
            {
                "evidence_ref": item.evidence_ref,
                "action_id": item.action_id,
                "outcome": item.outcome,
                "summary": item.summary,
                "violations": item.violations,
            }
            for item in state.observations
        ],
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
        applied = "finish_run"
        updated.phase = "finished"
        updated.termination_reason = "agent step budget exhausted after analyzer failure"
    elif updated.pending_actions:
        applied = "continue_plan"
        updated.phase = "executing"
        updated.termination_reason = None
    else:
        applied = "finish_run"
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
    """Atomically validate and apply one untrusted Analyzer decision."""
    updated = state.model_copy(deep=True)
    updated.remaining_step_budget -= 1
    candidate = updated.model_copy(deep=True)

    errors: list[str] = []
    compiled_case: CompiledTestCase | None = None
    compiled_workflow: CompiledWorkflow | None = None
    added_action: PlannedAction | None = None
    removed_actions: list[PlannedAction] = []
    applied = decision.decision
    evidence_refs = {item.evidence_ref for item in candidate.observations}

    if decision.new_hypothesis is not None:
        hypothesis = decision.new_hypothesis
        if hypothesis.status != "open":
            errors.append("a new hypothesis must start with status 'open'")
        elif any(
            item.hypothesis_id == hypothesis.hypothesis_id
            for item in candidate.hypotheses
        ):
            errors.append(
                f"hypothesis_id {hypothesis.hypothesis_id!r} already exists"
            )
        elif not hypothesis.supporting_evidence and not hypothesis.contradicting_evidence:
            errors.append("a new hypothesis must reference observation evidence")
        elif unknown := _unknown_evidence(hypothesis, evidence_refs):
            errors.append(f"hypothesis references unknown evidence {unknown!r}")
        else:
            candidate.hypotheses.append(hypothesis)

    if decision.hypothesis_update is not None:
        update = decision.hypothesis_update
        hypothesis = next(
            (
                item
                for item in candidate.hypotheses
                if item.hypothesis_id == update.hypothesis_id
            ),
            None,
        )
        if hypothesis is None:
            errors.append(
                f"unknown hypothesis_id {update.hypothesis_id!r}"
            )
        elif hypothesis.status != "open":
            errors.append(
                f"hypothesis {hypothesis.hypothesis_id!r} is already {hypothesis.status}"
            )
        elif not update.supporting_evidence and not update.contradicting_evidence:
            errors.append("a hypothesis update must reference new observation evidence")
        elif unknown := _unknown_evidence(update, evidence_refs):
            errors.append(f"hypothesis update references unknown evidence {unknown!r}")
        else:
            hypothesis.status = update.status
            hypothesis.confidence = update.confidence
            hypothesis.supporting_evidence = list(
                dict.fromkeys([*hypothesis.supporting_evidence, *update.supporting_evidence])
            )
            hypothesis.contradicting_evidence = list(
                dict.fromkeys(
                    [*hypothesis.contradicting_evidence, *update.contradicting_evidence]
                )
            )

    if decision.diagnostic_hypothesis_id is not None:
        target = next(
            (
                item
                for item in candidate.hypotheses
                if item.hypothesis_id == decision.diagnostic_hypothesis_id
            ),
            None,
        )
        if target is None:
            errors.append(
                f"diagnostic references unknown hypothesis_id "
                f"{decision.diagnostic_hypothesis_id!r}"
            )
        elif target.status != "open":
            errors.append(
                f"diagnostic hypothesis {target.hypothesis_id!r} is not open"
            )

    working_variables = candidate.unambiguous_variables()
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
            if not candidate.delete_approved:
                errors.append("diagnostic case requires ungranted DELETE approval")
        known_case_ids = existing_case_ids or set()
        planned_case_ids = {
            item.item_id
            for item in [*candidate.pending_actions, *candidate.completed_actions]
            if item.kind == "test_case"
        }
        if compiled_case is not None and compiled_case.case_id in known_case_ids | planned_case_ids:
            errors.append("diagnostic case_id already exists in the Agent plan")
        fingerprint = request_fingerprint(compiled_case) if compiled_case is not None else None
        if fingerprint is not None and fingerprint in candidate.executed_fingerprints:
            if decision.diagnostic_case.repeat != "once":
                errors.append("diagnostic case duplicates an executed request")
            elif fingerprint in candidate.repeated_fingerprints:
                errors.append("diagnostic request fingerprint was already repeated once")
        elif compiled_case is not None and decision.diagnostic_case.repeat == "once":
            errors.append("diagnostic repeat=once requires an executed request fingerprint")
        if compiled_case is not None and candidate.remaining_test_budget < 1:
            errors.append("diagnostic case exceeds remaining request budget")
        if compiled_case is not None and candidate.remaining_step_budget < 1:
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
                hypothesis_id=decision.diagnostic_hypothesis_id,
                allow_repeat=decision.diagnostic_case.repeat == "once",
            )
            candidate.pending_actions.insert(0, added_action)
            candidate.plan_version += 1

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
            and not candidate.delete_approved
        ):
            errors.append("diagnostic workflow requires ungranted DELETE approval")
        cost = len(compiled_workflow.steps) if compiled_workflow is not None else 0
        if compiled_workflow is not None and cost > candidate.remaining_test_budget:
            errors.append("diagnostic workflow exceeds remaining request budget")
        if compiled_workflow is not None and cost > candidate.remaining_step_budget:
            errors.append("diagnostic workflow exceeds remaining agent step budget")
        known_ids = existing_workflow_ids or set()
        planned_ids = {
            item.item_id
            for item in [*candidate.pending_actions, *candidate.completed_actions]
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
                hypothesis_id=decision.diagnostic_hypothesis_id,
            )
            candidate.pending_actions.insert(0, added_action)
            candidate.plan_version += 1

    elif decision.decision == "revise_pending_actions":
        pending = {
            (item.kind, item.item_id): item for item in candidate.pending_actions
        }
        requested = [
            (item.kind, item.item_id) for item in decision.pending_revision.actions
        ]
        unknown = [identity for identity in requested if identity not in pending]
        if unknown:
            errors.append(f"pending revision references unknown action(s) {unknown!r}")
        else:
            revised = [pending[identity] for identity in requested]
            retained = set(requested)
            removed_actions = [
                item
                for item in candidate.pending_actions
                if (item.kind, item.item_id) not in retained
            ]
            if revised != candidate.pending_actions:
                candidate.pending_actions = revised
                candidate.plan_version += 1

    elif decision.decision == "finish_run" and candidate.pending_actions:
        update = decision.hypothesis_update
        if update is None or update.status != "confirmed" or update.confidence != "high":
            errors.append(
                "finish_run with pending actions requires a high-confidence confirmed "
                "hypothesis_update"
            )

    if errors:
        applied = "continue_plan"
        compiled_case = None
        compiled_workflow = None
        added_action = None
        removed_actions = []
    else:
        updated = candidate

    if applied == "finish_run":
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
            removed_actions=removed_actions,
        ),
        diagnostic_case=compiled_case,
        diagnostic_workflow=compiled_workflow,
    )


def _unknown_evidence(hypothesis: object, known: set[str]) -> str | None:
    refs = [
        *getattr(hypothesis, "supporting_evidence", []),
        *getattr(hypothesis, "contradicting_evidence", []),
    ]
    return next((item for item in refs if item not in known), None)
