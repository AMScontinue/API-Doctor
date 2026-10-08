"""Typed contracts for repeatable end-to-end Agent benchmarks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedSignal(BenchmarkModel):
    operation_id: str
    outcome: Literal["failed", "error"]
    actual_status: int | None = None
    expected_status: int | None = None


class ScenarioExpectations(BenchmarkModel):
    required_signals: list[ExpectedSignal] = Field(default_factory=list)
    max_failed_observations: int | None = Field(default=None, ge=0)
    max_error_observations: int | None = Field(default=None, ge=0)
    max_confirmed_findings: int | None = Field(default=None, ge=0)
    min_confirmed_findings: int = Field(default=0, ge=0)
    min_reported_findings: int = Field(default=0, ge=0)
    min_analyzer_decisions: int = Field(default=0, ge=0)
    min_diagnostic_actions: int = Field(default=0, ge=0)
    max_diagnostic_actions: int | None = Field(default=None, ge=0)
    require_confirmed_finding: bool = False
    require_report_without_fallback: bool = True


class BenchmarkScenario(BenchmarkModel):
    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    description: str
    defects: list[str] = Field(default_factory=list)
    start_service: bool = True
    max_tests: int = Field(gt=0)
    max_steps: int = Field(gt=0)
    expectations: ScenarioExpectations


class AgentBenchmarkSuite(BenchmarkModel):
    spec: str
    rules: str
    service: Literal["buggy_shop"] = "buggy_shop"
    service_dir: str
    base_url: str
    scenarios: list[BenchmarkScenario] = Field(min_length=1)


class BenchmarkCheck(BenchmarkModel):
    name: str
    passed: bool
    details: str


class BenchmarkMetrics(BenchmarkModel):
    observations: int
    passed_observations: int
    failed_observations: int
    error_observations: int
    blocked_observations: int
    http_requests: int
    consumed_agent_steps: int
    analyzer_decisions: int
    analyzer_llm_calls: int
    diagnostic_actions: int
    confirmed_findings: int
    possible_findings: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    structure_repair_attempts: int
    planner_fallback: bool
    report_fallback: bool


class ScenarioResult(BenchmarkModel):
    scenario_id: str
    repetition: int = Field(ge=1)
    passed: bool
    run_id: str | None = None
    artifact_directory: str
    duration_seconds: float = Field(ge=0)
    checks: list[BenchmarkCheck]
    metrics: BenchmarkMetrics | None = None
    execution_error: str | None = None


class BenchmarkSummary(BenchmarkModel):
    suite: str
    repetitions: int
    total_runs: int
    passed_runs: int
    failed_runs: int
    duration_seconds: float = Field(ge=0)
    scenario_pass_rates: dict[str, float]
    total_llm_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    results: list[ScenarioResult]
