"""Structured records for deterministic baseline runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api_doctor.testspec.models import CompiledTestCase, SemanticTestCandidate, TestResult


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
