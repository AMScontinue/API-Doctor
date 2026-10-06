"""Typed working-memory and planning contracts for one Agent run."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from api_doctor.testspec.models import StrictModel


class PlannedAction(StrictModel):
    kind: Literal["test_case", "workflow"]
    item_id: str
    priority: int = Field(ge=0, le=100)
    reason: str
    estimated_requests: int = Field(gt=0)


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


class AgentHypothesis(StrictModel):
    statement: str
    confidence: Literal["low", "medium", "high"]
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)


class AgentState(StrictModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    spec_fingerprint: str
    phase: Literal["planning", "executing", "analyzing", "finished"] = "planning"
    plan_version: int = Field(default=1, ge=1)
    strategy: str
    pending_actions: list[PlannedAction]
    completed_actions: list[PlannedAction] = Field(default_factory=list)
    observations: list[AgentObservation] = Field(default_factory=list)
    hypotheses: list[AgentHypothesis] = Field(default_factory=list)
    remaining_test_budget: int = Field(ge=0)
    remaining_step_budget: int = Field(ge=0)
    delete_approved: bool = False
    executed_fingerprints: set[str] = Field(default_factory=set)
    termination_reason: str | None = None


class InitialPlanningResult(StrictModel):
    proposed_plan: InitialPlanProposal
    state: AgentState
    used_fallback: bool = False
    validation_errors: list[str] = Field(default_factory=list)
