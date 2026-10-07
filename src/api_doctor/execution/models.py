"""Structured records for deterministic baseline runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api_doctor.testspec.models import CompiledTestCase, SemanticTestCandidate, TestResult
from api_doctor.workflows.validation import CompiledWorkflowStep


class RunModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseRecord(RunModel):
    candidate: SemanticTestCandidate
    state: Literal["executed", "blocked", "compile_error"]
    compiled_case: CompiledTestCase | None = None
    result: TestResult | None = None
    reason: str | None = None


class RunMetrics(RunModel):
    operations: int
    candidates: int
    compilable_candidates: int
    compiled: int
    executed: int
    passed: int
    failed: int
    errors: int
    blocked: int


class RunConfig(RunModel):
    """Immutable inputs shared by every step of one deterministic run."""

    base_url: str
    seed: int
    time_anchor: datetime
    max_tests: int = Field(gt=0)
    approve_delete: bool = False
    timeout_seconds: float = Field(default=5.0, gt=0)


class WorkingVariable(RunModel):
    consumer_operation_id: str
    parameter_location: str
    parameter_name: str
    value: object
    producer_operation_id: str
    producer_case_id: str
    response_expression: str
    link_source: str


class BaselineRun(RunModel):
    run_id: str
    mode: Literal["deterministic"] = "deterministic"
    started_at: datetime
    finished_at: datetime
    spec_source: str
    spec_fingerprint: str
    base_url: str
    seed: int
    time_anchor: datetime
    max_tests: int
    metrics: RunMetrics
    variables: list[WorkingVariable] = Field(default_factory=list)
    records: list[CaseRecord] = Field(default_factory=list)


class WorkflowVariable(RunModel):
    name: str
    value: object
    producer_step_id: str
    producer_operation_id: str
    producer_case_id: str
    response_expression: str
    response_status: int


class WorkflowInputVariable(RunModel):
    name: str
    value: object
    producer_operation_id: str
    producer_case_id: str
    response_expression: str


class WorkflowStepRecord(RunModel):
    step: CompiledWorkflowStep
    state: Literal["executed", "blocked", "compile_error"]
    compiled_case: CompiledTestCase | None = None
    result: TestResult | None = None
    extracted_variables: list[str] = Field(default_factory=list)
    reason: str | None = None


class WorkflowRunMetrics(RunModel):
    total_steps: int
    executed: int
    passed: int
    failed: int
    errors: int
    blocked: int


class WorkflowRun(RunModel):
    run_id: str
    mode: Literal["workflow"] = "workflow"
    workflow_id: str
    started_at: datetime
    finished_at: datetime
    spec_source: str
    spec_fingerprint: str
    base_url: str
    seed: int
    time_anchor: datetime
    max_tests: int
    metrics: WorkflowRunMetrics
    variables: list[WorkflowVariable] = Field(default_factory=list)
    records: list[WorkflowStepRecord] = Field(default_factory=list)
