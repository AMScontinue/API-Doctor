"""Score saved in-memory Agent products without asking an LLM to judge itself."""

from __future__ import annotations

from dataclasses import dataclass

from api_doctor.agent.pipeline import AgentPipelineResult
from api_doctor.evaluation.agent_models import (
    BenchmarkCheck,
    BenchmarkMetrics,
    BenchmarkScenario,
    ExpectedSignal,
    ScenarioResult,
)


@dataclass(frozen=True)
class ObservedSignal:
    evidence_ref: str
    operation_id: str
    outcome: str
    actual_status: int | None
    expected_statuses: list[int]


def score_scenario(
    scenario: BenchmarkScenario,
    repetition: int,
    result: AgentPipelineResult,
    artifact_directory: str,
) -> ScenarioResult:
    signals = _observed_signals(result)
    state = result.run.state
    expectations = scenario.expectations
    counts = {
        outcome: sum(item.outcome == outcome for item in state.observations)
        for outcome in ("passed", "failed", "error", "blocked")
    }
    diagnostics = sum(item.origin == "diagnostic" for item in state.completed_actions)
    analyzer_decisions = len(result.run.analyses)
    analyzer_llm_calls = result.model_usage["analyzer"].calls
    finding_refs = {
        ref
        for finding in result.reporting.analysis.confirmed_findings
        for ref in finding.evidence_refs
    }
    checks: list[BenchmarkCheck] = []

    matched_signal_refs: set[str] = set()
    for expected in expectations.required_signals:
        matches = [item for item in signals if _matches_signal(item, expected)]
        matched_signal_refs.update(item.evidence_ref for item in matches)
        checks.append(
            BenchmarkCheck(
                name=f"required_signal:{expected.operation_id}",
                passed=bool(matches),
                details=(
                    f"matched {len(matches)} execution result(s)"
                    if matches
                    else "no matching real HTTP execution result"
                ),
            )
        )
    _append_max_check(
        checks,
        "max_failed_observations",
        counts["failed"],
        expectations.max_failed_observations,
    )
    _append_max_check(
        checks,
        "max_error_observations",
        counts["error"],
        expectations.max_error_observations,
    )
    _append_max_check(
        checks,
        "max_confirmed_findings",
        len(result.reporting.analysis.confirmed_findings),
        expectations.max_confirmed_findings,
    )
    checks.append(
        BenchmarkCheck(
            name="minimum_confirmed_findings",
            passed=(
                len(result.reporting.analysis.confirmed_findings)
                >= expectations.min_confirmed_findings
            ),
            details=(
                f"observed={len(result.reporting.analysis.confirmed_findings)}, "
                f"required>={expectations.min_confirmed_findings}"
            ),
        )
    )
    reported_findings = (
        len(result.reporting.analysis.confirmed_findings)
        + len(result.reporting.analysis.possible_findings)
    )
    checks.append(
        BenchmarkCheck(
            name="minimum_reported_findings",
            passed=reported_findings >= expectations.min_reported_findings,
            details=(
                f"observed={reported_findings}, "
                f"required>={expectations.min_reported_findings}"
            ),
        )
    )
    checks.append(
        BenchmarkCheck(
            name="minimum_analyzer_decisions",
            passed=analyzer_decisions >= expectations.min_analyzer_decisions,
            details=(
                f"observed={analyzer_decisions}, "
                f"required>={expectations.min_analyzer_decisions}"
            ),
        )
    )
    checks.append(
        BenchmarkCheck(
            name="minimum_diagnostic_actions",
            passed=diagnostics >= expectations.min_diagnostic_actions,
            details=f"observed={diagnostics}, required>={expectations.min_diagnostic_actions}",
        )
    )
    if expectations.max_diagnostic_actions is not None:
        checks.append(
            BenchmarkCheck(
                name="maximum_diagnostic_actions",
                passed=diagnostics <= expectations.max_diagnostic_actions,
                details=(
                    f"observed={diagnostics}, allowed<="
                    f"{expectations.max_diagnostic_actions}"
                ),
            )
        )
    if expectations.require_confirmed_finding:
        checks.append(
            BenchmarkCheck(
                name="confirmed_finding_for_required_signal",
                passed=bool(matched_signal_refs & finding_refs),
                details=(
                    "a confirmed finding cites the defect signal"
                    if matched_signal_refs & finding_refs
                    else "no confirmed finding cites a matched defect signal"
                ),
            )
        )
    checks.extend(
        [
            BenchmarkCheck(
                name="within_request_budget",
                passed=state.remaining_test_budget >= 0,
                details=f"remaining={state.remaining_test_budget}",
            ),
            BenchmarkCheck(
                name="within_agent_step_budget",
                passed=state.remaining_step_budget >= 0,
                details=f"remaining={state.remaining_step_budget}",
            ),
            BenchmarkCheck(
                name="report_without_fallback",
                passed=(
                    not expectations.require_report_without_fallback
                    or not result.reporting.used_fallback
                ),
                details=f"used_fallback={result.reporting.used_fallback}",
            ),
        ]
    )

    usage = result.model_usage["total"]
    metrics = BenchmarkMetrics(
        observations=len(state.observations),
        passed_observations=counts["passed"],
        failed_observations=counts["failed"],
        error_observations=counts["error"],
        blocked_observations=counts["blocked"],
        http_requests=sum(item.request_count for item in state.observations),
        consumed_agent_steps=(
            1
            + sum(item.agent_steps for item in state.observations)
            + len(result.run.analyses)
        ),
        analyzer_decisions=analyzer_decisions,
        analyzer_llm_calls=analyzer_llm_calls,
        diagnostic_actions=diagnostics,
        confirmed_findings=len(result.reporting.analysis.confirmed_findings),
        possible_findings=len(result.reporting.analysis.possible_findings),
        llm_calls=usage.calls,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        structure_repair_attempts=usage.structure_repair_attempts,
        planner_fallback=result.planning.used_fallback,
        report_fallback=result.reporting.used_fallback,
    )
    return ScenarioResult(
        scenario_id=scenario.id,
        repetition=repetition,
        passed=all(item.passed for item in checks),
        run_id=state.run_id,
        artifact_directory=artifact_directory,
        duration_seconds=0,
        checks=checks,
        metrics=metrics,
    )


def _observed_signals(result: AgentPipelineResult) -> list[ObservedSignal]:
    signals: list[ObservedSignal] = []
    for record in result.run.records:
        if record.test_case is not None and record.test_result is not None:
            signals.append(
                ObservedSignal(
                    evidence_ref=record.observation.evidence_ref,
                    operation_id=record.test_case.operation_id,
                    outcome=record.test_result.outcome,
                    actual_status=record.test_result.actual_status,
                    expected_statuses=record.test_result.expected_statuses,
                )
            )
        if record.workflow_run is not None:
            for step in record.workflow_run.records:
                if step.compiled_case is None or step.result is None:
                    continue
                signals.append(
                    ObservedSignal(
                        evidence_ref=record.observation.evidence_ref,
                        operation_id=step.compiled_case.operation_id,
                        outcome=step.result.outcome,
                        actual_status=step.result.actual_status,
                        expected_statuses=step.result.expected_statuses,
                    )
                )
    return signals


def _matches_signal(actual: ObservedSignal, expected: ExpectedSignal) -> bool:
    return (
        actual.operation_id == expected.operation_id
        and actual.outcome == expected.outcome
        and (expected.actual_status is None or actual.actual_status == expected.actual_status)
        and (
            expected.expected_status is None
            or expected.expected_status in actual.expected_statuses
        )
    )


def _append_max_check(
    checks: list[BenchmarkCheck],
    name: str,
    actual: int,
    maximum: int | None,
) -> None:
    if maximum is None:
        return
    checks.append(
        BenchmarkCheck(
            name=name,
            passed=actual <= maximum,
            details=f"observed={actual}, allowed<={maximum}",
        )
    )
