from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api_doctor.agent.models import (
    AgentActionRecord,
    AgentObservation,
    AgentRun,
    AgentState,
    PlannedAction,
)
from api_doctor.agent.reporting import (
    ReportAnalysis,
    ReportFinding,
    ReportSynthesisResult,
)
from api_doctor.evaluation.agent_models import BenchmarkScenario
from api_doctor.evaluation.agent_runner import _allocate_base_url
from api_doctor.evaluation.agent_scoring import score_scenario
from api_doctor.llm.gateway import ModelCallTelemetry


def _pipeline_result(*, report_ref: str):
    action = PlannedAction(
        kind="test_case",
        item_id="TC-confirm",
        priority=80,
        reason="exercise the transition",
        estimated_requests=1,
    )
    observation = AgentObservation(
        action_kind="test_case",
        action_id=action.item_id,
        outcome="failed",
        evidence_ref="trajectory:actions/1",
        summary="PATCH returned 409; expected 200",
        request_count=1,
    )
    record = AgentActionRecord(
        action=action,
        observation=observation,
    )
    record.test_case = SimpleNamespace(operation_id="updateOrderStatus")
    record.test_result = SimpleNamespace(
        outcome="failed",
        actual_status=409,
        expected_statuses=[200],
    )
    state = AgentState(
        spec_fingerprint="spec",
        phase="finished",
        strategy="benchmark",
        pending_actions=[],
        completed_actions=[action],
        observations=[observation],
        remaining_test_budget=0,
        remaining_step_budget=1,
    )
    reporting = ReportSynthesisResult(
        analysis=ReportAnalysis(
            executive_summary="one defect",
            confirmed_findings=[
                ReportFinding(
                    title="transition defect",
                    severity="high",
                    summary="confirmed",
                    evidence_refs=[report_ref],
                    recommendation="fix and rerun",
                )
            ],
        )
    )
    return SimpleNamespace(
        run=AgentRun(state=state, records=[record]),
        reporting=reporting,
        planning=SimpleNamespace(used_fallback=False),
        model_usage={
            "analyzer": ModelCallTelemetry(calls=1, duration_ms=1),
            "total": ModelCallTelemetry(
                calls=5,
                duration_ms=5,
                prompt_tokens=100,
                completion_tokens=20,
            ),
        },
    )


def _scenario() -> BenchmarkScenario:
    return BenchmarkScenario.model_validate(
        {
            "id": "confirm_conflict",
            "description": "detect a broken transition",
            "defects": ["confirm-conflict"],
            "max_tests": 1,
            "max_steps": 3,
            "expectations": {
                "required_signals": [
                    {
                        "operation_id": "updateOrderStatus",
                        "outcome": "failed",
                        "actual_status": 409,
                        "expected_status": 200,
                    }
                ],
                "min_analyzer_decisions": 0,
                "require_confirmed_finding": True,
            },
        }
    )


def test_scorer_requires_real_http_signal_and_linked_report_evidence():
    scored = score_scenario(
        _scenario(),
        1,
        _pipeline_result(report_ref="trajectory:actions/1"),
        "runs/benchmark/confirm",
    )

    assert scored.passed is True
    assert scored.metrics.http_requests == 1
    assert scored.metrics.llm_calls == 5
    assert all(item.passed for item in scored.checks)


def test_scorer_rejects_report_that_does_not_cite_the_defect_signal():
    scored = score_scenario(
        _scenario(),
        1,
        _pipeline_result(report_ref="trajectory:actions/2"),
        "runs/benchmark/confirm",
    )

    check = next(
        item for item in scored.checks if item.name == "confirmed_finding_for_required_signal"
    )
    assert check.passed is False
    assert scored.passed is False


def test_benchmark_allocates_an_isolated_local_port():
    listener = MagicMock()
    listener.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 49152)
    with patch("api_doctor.evaluation.agent_runner.socket.socket", return_value=listener):
        allocated = _allocate_base_url("http://127.0.0.1:18080")

    assert allocated == "http://127.0.0.1:49152"
