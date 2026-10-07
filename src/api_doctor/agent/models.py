"""Typed working-memory and planning contracts for one Agent run."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from api_doctor.execution.models import WorkflowRun
from api_doctor.generation.workflow_models import ConstantAssignment, WorkflowCandidate
from api_doctor.testspec.models import CompiledTestCase, StrictModel, TestResult


class PlannedAction(StrictModel):
    kind: Literal["test_case", "workflow"]
    item_id: str
    priority: int = Field(ge=0, le=100)
    reason: str
    estimated_requests: int = Field(gt=0)
    origin: Literal["initial_plan", "diagnostic"] = "initial_plan"
    hypothesis_id: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    allow_repeat: bool = False


class InitialPlanProposal(StrictModel):
    strategy: str
    selected: list[PlannedAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_items(self) -> "InitialPlanProposal":
        identities = [(item.kind, item.item_id) for item in self.selected]
        if len(identities) != len(set(identities)):
            raise ValueError("initial plan contains duplicate action references")
        return self


class AgentObservation(StrictModel):
    action_kind: Literal["test_case", "workflow"]
    action_id: str
    outcome: Literal["passed", "failed", "error", "blocked"]
    evidence_ref: str
    summary: str
    request_count: int = Field(default=0, ge=0)
    agent_steps: int = Field(default=1, ge=1)
    violations: list[str] = Field(default_factory=list)


class AgentVariable(StrictModel):
    action_id: str
    name: str
    value: object
    producer_operation_id: str
    producer_case_id: str
    response_expression: str


class AgentHypothesis(StrictModel):
    hypothesis_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    statement: str
    status: Literal["open", "confirmed", "rejected"] = "open"
    confidence: Literal["low", "medium", "high"]
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)


class HypothesisUpdate(StrictModel):
    hypothesis_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    status: Literal["open", "confirmed", "rejected"]
    confidence: Literal["low", "medium", "high"]
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    rationale: str


class DiagnosticBinding(StrictModel):
    location: Literal["path", "query", "header", "cookie", "body"]
    name: str
    variable: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class DiagnosticCaseProposal(StrictModel):
    case_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    operation_id: str
    purpose: str
    expected_status_codes: list[int] = Field(min_length=1)
    repeat: Literal["forbid", "once"] = "forbid"
    bindings: list[DiagnosticBinding] = Field(default_factory=list)
    assignments: list[ConstantAssignment] = Field(default_factory=list)


class ActionReference(StrictModel):
    kind: Literal["test_case", "workflow"]
    item_id: str


class PendingPlanRevision(StrictModel):
    """A reordered subset of the already trusted pending queue."""

    actions: list[ActionReference]
    rationale: str

    @model_validator(mode="after")
    def unique_actions(self) -> "PendingPlanRevision":
        identities = [(item.kind, item.item_id) for item in self.actions]
        if len(identities) != len(set(identities)):
            raise ValueError("pending plan revision contains duplicate action references")
        return self


class AnalysisDecision(StrictModel):
    decision: Literal[
        "continue_plan",
        "add_diagnostic_case",
        "add_diagnostic_workflow",
        "revise_pending_actions",
        "finish_investigation",
        "finish_run",
    ]
    reason: str
    new_hypothesis: AgentHypothesis | None = None
    hypothesis_update: HypothesisUpdate | None = None
    diagnostic_hypothesis_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$"
    )
    diagnostic_case: DiagnosticCaseProposal | None = None
    diagnostic_workflow: WorkflowCandidate | None = None
    pending_revision: PendingPlanRevision | None = None

    @model_validator(mode="after")
    def decision_payload_matches_action(self) -> "AnalysisDecision":
        expected_case = self.decision == "add_diagnostic_case"
        expected_workflow = self.decision == "add_diagnostic_workflow"
        expected_revision = self.decision == "revise_pending_actions"
        if (self.diagnostic_case is not None) != expected_case:
            raise ValueError("diagnostic_case is required only for add_diagnostic_case")
        if (self.diagnostic_workflow is not None) != expected_workflow:
            raise ValueError("diagnostic_workflow is required only for add_diagnostic_workflow")
        if (self.pending_revision is not None) != expected_revision:
            raise ValueError("pending_revision is required only for revise_pending_actions")
        if self.new_hypothesis is not None and self.hypothesis_update is not None:
            raise ValueError("an analysis decision cannot create and update a hypothesis together")
        if self.new_hypothesis is not None and self.new_hypothesis.status != "open":
            raise ValueError("a new hypothesis must start with status 'open'")
        expects_hypothesis = expected_case or expected_workflow
        if (self.diagnostic_hypothesis_id is not None) != expects_hypothesis:
            raise ValueError(
                "diagnostic_hypothesis_id is required only for diagnostic decisions"
            )
        if self.decision == "finish_investigation" and (
            self.hypothesis_update is None
            or self.hypothesis_update.status not in {"confirmed", "rejected"}
        ):
            raise ValueError(
                "finish_investigation requires a confirmed or rejected hypothesis_update"
            )
        return self


class AnalysisRecord(StrictModel):
    proposed: AnalysisDecision | None = None
    applied_decision: Literal[
        "continue_plan",
        "add_diagnostic_case",
        "add_diagnostic_workflow",
        "revise_pending_actions",
        "finish_investigation",
        "finish_run",
    ]
    validation_errors: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)
    added_action: PlannedAction | None = None
    removed_actions: list[PlannedAction] = Field(default_factory=list)
    analyzer_error: str | None = None


class AgentState(StrictModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    spec_fingerprint: str
    phase: Literal["planning", "executing", "analyzing", "finished"] = "planning"
    plan_version: int = Field(default=1, ge=1)
    strategy: str
    pending_actions: list[PlannedAction]
    completed_actions: list[PlannedAction] = Field(default_factory=list)
    observations: list[AgentObservation] = Field(default_factory=list)
    variables: list[AgentVariable] = Field(default_factory=list)
    hypotheses: list[AgentHypothesis] = Field(default_factory=list)
    remaining_test_budget: int = Field(ge=0)
    remaining_step_budget: int = Field(ge=0)
    delete_approved: bool = False
    executed_fingerprints: set[str] = Field(default_factory=set)
    repeated_fingerprints: set[str] = Field(default_factory=set)
    termination_reason: str | None = None

    def unambiguous_variables(self) -> dict[str, AgentVariable]:
        """Expose stable keys for every value without allowing ambiguous lookup."""
        grouped: dict[str, list[AgentVariable]] = {}
        for variable in self.variables:
            grouped.setdefault(variable.name, []).append(variable)
        available: dict[str, AgentVariable] = {}
        for index, variable in enumerate(self.variables, start=1):
            if len(grouped[variable.name]) == 1:
                key = variable.name
            else:
                safe_name = "".join(
                    character if character.isalnum() or character == "_" else "_"
                    for character in variable.name
                )
                key = f"memory_{index}_{safe_name}"
            available[key] = variable
        return available


class InitialPlanningResult(StrictModel):
    proposed_plan: InitialPlanProposal
    state: AgentState
    used_fallback: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)


class AgentActionRecord(StrictModel):
    action: PlannedAction
    observation: AgentObservation
    request_fingerprints: list[str] = Field(default_factory=list)
    test_case: CompiledTestCase | None = None
    test_result: TestResult | None = None
    workflow_run: WorkflowRun | None = None


class AgentRun(StrictModel):
    state: AgentState
    records: list[AgentActionRecord] = Field(default_factory=list)
    analyses: list[AnalysisRecord] = Field(default_factory=list)
