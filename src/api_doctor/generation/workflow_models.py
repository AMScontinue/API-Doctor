"""Typed, non-executable workflow proposals produced by an LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


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

    @model_validator(mode="after")
    def validate_variable_order(self) -> "WorkflowCandidate":
        step_ids: set[str] = set()
        available: set[str] = set()
        prior_operations: set[str] = set()
        for step in self.steps:
            if step.step_id in step_ids:
                raise ValueError(f"duplicate step_id {step.step_id!r}")
            step_ids.add(step.step_id)
            for binding in step.bind:
                if binding.source == "workflow" and binding.variable not in available:
                    raise ValueError(
                        f"variable {binding.variable!r} is used before it is extracted"
                    )
            for extraction in step.extract:
                if extraction.variable in available:
                    raise ValueError(f"duplicate variable {extraction.variable!r}")
                available.add(extraction.variable)
            if step.repeat == "once":
                if step.operation_id not in prior_operations:
                    raise ValueError(
                        f"step {step.step_id!r} cannot repeat operation "
                        f"{step.operation_id!r} before an earlier step uses it"
                    )
                if step.expected_status_codes is None:
                    raise ValueError(
                        f"step {step.step_id!r} must declare expected_status_codes "
                        "when repeat=once"
                    )
            prior_operations.add(step.operation_id)
        return self


class WorkflowDiscovery(WorkflowModel):
    workflows: list[WorkflowCandidate] = Field(default_factory=list, max_length=10)
    ambiguities: list[str] = Field(default_factory=list)
