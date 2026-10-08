from api_doctor.agent.models import (
    AgentHypothesis,
    AgentObservation,
    AgentRun,
    AgentState,
)
from api_doctor.agent.reporting import (
    ReportAnalysis,
    build_report_prompt,
    synthesize_report,
)
from api_doctor.generation.semantic_discovery import SemanticDiscovery
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelResponse
from api_doctor.testspec.fusion import FinalTestPlan
from api_doctor.workflows.validation import WorkflowValidationResult


def _run() -> AgentRun:
    observation = AgentObservation(
        action_kind="test_case",
        action_id="TC-confirm",
        outcome="failed",
        evidence_ref="trajectory:actions/1",
        summary="PATCH returned 409; expected 200",
        request_count=1,
        violations=["unexpected status 409"],
    )
    state = AgentState(
        spec_fingerprint="spec",
        phase="finished",
        strategy="test",
        pending_actions=[],
        observations=[observation],
        hypotheses=[
            AgentHypothesis(
                hypothesis_id="H-confirm",
                statement="the valid confirm transition is rejected",
                status="confirmed",
                confidence="high",
                supporting_evidence=[observation.evidence_ref],
            )
        ],
        remaining_test_budget=0,
        remaining_step_budget=1,
        termination_reason="pending action queue exhausted",
    )
    return AgentRun(state=state)


def _inputs():
    return (
        SemanticDiscovery(candidates=[], ambiguities=["refund timing is unspecified"]),
        WorkflowDiscovery(workflows=[], ambiguities=[]),
        WorkflowValidationResult(reviews=[]),
        FinalTestPlan(cases=[], conflicts=[]),
    )


class StubGateway:
    def __init__(self, analysis: ReportAnalysis | None = None, error: Exception | None = None):
        self.analysis = analysis
        self.error = error
        self.prompt = None

    def complete(self, prompt, *, response_model, trace_label):
        self.prompt = prompt
        assert response_model is ReportAnalysis
        assert trace_label == "report-synthesizer"
        if self.error is not None:
            raise self.error
        return ModelResponse(
            value=self.analysis,
            telemetry=ModelCallTelemetry(calls=1, duration_ms=3),
        )


def test_report_prompt_contains_compact_fact_and_source_registries():
    run = _run()
    prompt = build_report_prompt(run, *_inputs())

    refs = {item["source_ref"] for item in prompt["source_registry"]}
    assert "trajectory:actions/1" in refs
    assert "hypothesis:H-confirm" in refs
    assert "semantic-ambiguity:1" in refs
    assert prompt["run_summary"]["outcome_counts"]["failed"] == 1
    assert "records" not in prompt


def test_accepts_evidence_bound_report_analysis():
    analysis = ReportAnalysis.model_validate(
        {
            "executive_summary": "The confirm transition violated the contract.",
            "confirmed_findings": [
                {
                    "title": "Confirm transition rejected",
                    "severity": "high",
                    "summary": "A documented transition returned 409.",
                    "evidence_refs": ["trajectory:actions/1"],
                    "hypothesis_id": "H-confirm",
                    "recommendation": "Fix the transition and rerun the workflow.",
                }
            ],
            "next_steps": [
                {
                    "title": "Rerun",
                    "summary": "Repeat after remediation.",
                    "source_refs": ["run:termination"],
                }
            ],
        }
    )
    gateway = StubGateway(analysis)

    outcome = synthesize_report(_run(), *_inputs(), gateway)

    assert outcome.result.used_fallback is False
    assert outcome.result.validation_errors == []
    assert outcome.result.analysis.confirmed_findings[0].hypothesis_id == "H-confirm"
    assert outcome.telemetry.calls == 1


def test_unknown_report_evidence_triggers_deterministic_fallback():
    analysis = ReportAnalysis.model_validate(
        {
            "executive_summary": "Invented conclusion.",
            "confirmed_findings": [
                {
                    "title": "Invented defect",
                    "severity": "high",
                    "summary": "Unsupported.",
                    "evidence_refs": ["trajectory:actions/999"],
                    "recommendation": "Do something unsupported.",
                }
            ],
        }
    )

    outcome = synthesize_report(_run(), *_inputs(), StubGateway(analysis))

    assert outcome.result.used_fallback is True
    assert "unknown source" in outcome.result.validation_errors[0]
    assert outcome.result.analysis.confirmed_findings[0].evidence_refs == [
        "trajectory:actions/1"
    ]


def test_omitted_failure_triggers_deterministic_fallback():
    analysis = ReportAnalysis(executive_summary="The run completed.")

    outcome = synthesize_report(_run(), *_inputs(), StubGateway(analysis))

    assert outcome.result.used_fallback is True
    assert "omits failed/error observation" in outcome.result.validation_errors[0]
    assert outcome.result.analysis.confirmed_findings[0].evidence_refs == [
        "trajectory:actions/1"
    ]


def test_provider_failure_keeps_a_deterministic_final_report():
    outcome = synthesize_report(
        _run(),
        *_inputs(),
        StubGateway(error=TimeoutError("sensitive provider detail")),
    )

    assert outcome.result.used_fallback is True
    assert outcome.result.synthesizer_error == "Report synthesizer unavailable (TimeoutError)"
    assert "sensitive" not in outcome.result.synthesizer_error
    assert outcome.telemetry.calls == 0
