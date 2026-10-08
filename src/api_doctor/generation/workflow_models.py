"""Typed, non-executable workflow proposals produced by an LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VariableExtraction(WorkflowModel):
    variable: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    response_expression: str = Field(pattern=r"^\$response\.body#(?:/.*)?$")


class ParameterBinding(WorkflowModel):
    location: Literal["path", "query", "header", "cookie", "body"]
    name: str
    variable: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    source: Literal["workflow", "working_memory"] = "workflow"


class ConstantAssignment(WorkflowModel):
    location: Literal["path", "query", "header", "cookie", "body"]
    name: str
    value: JsonValue


class WorkflowStep(WorkflowModel):
    step_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    operation_id: str
    purpose: str
    expected_status_codes: list[int] | None = Field(default=None, min_length=1)
    repeat: Literal["forbid", "once"] = "forbid"
    extract: list[VariableExtraction] = Field(default_factory=list)
    bind: list[ParameterBinding] = Field(default_factory=list)
    assign: list[ConstantAssignment] = Field(default_factory=list)


class WorkflowCandidate(WorkflowModel):
    workflow_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    purpose: str
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    steps: list[WorkflowStep] = Field(min_length=2, max_length=4)


class WorkflowDiscovery(WorkflowModel):
    workflows: list[WorkflowCandidate] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
