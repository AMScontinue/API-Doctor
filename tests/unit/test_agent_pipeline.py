from datetime import datetime
from pathlib import Path

from api_doctor.agent.models import (
    AnalysisDecision,
    AgentActionRecord,
    AgentObservation,
    AgentRun,
    InitialPlanProposal,
)
from api_doctor.agent.persistence import AgentArtifactStore
from api_doctor.agent.pipeline import AgentPipeline
from api_doctor.execution.models import RunConfig
from api_doctor.generation.semantic_discovery import SemanticDiscovery
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelResponse
from api_doctor.openapi import load_openapi


ROOT = Path(__file__).resolve().parents[2]


class PipelineGateway:
    def __init__(self, *, fail_analyzer: bool = False) -> None:
        self.calls: list[str] = []
        self.analysis_calls = 0
        self.fail_analyzer = fail_analyzer

    def complete(self, prompt, *, response_model, trace_label):
        self.calls.append(trace_label)
        if response_model is SemanticDiscovery:
            value = SemanticDiscovery(candidates=[], ambiguities=[])
        elif response_model is WorkflowDiscovery:
            value = WorkflowDiscovery.model_validate(
                {
                    "workflows": [
                        {
                            "workflow_id": "invalid_workflow",
                            "purpose": "exercise an unknown operation",
                            "confidence": "low",
                            "reasoning": "test validation boundary",
                            "steps": [
                                {
                                    "step_id": "unknown-1",
                                    "operation_id": "notInCatalog",
                                    "purpose": "must be rejected",
                                },
                                {
                                    "step_id": "unknown-2",
                                    "operation_id": "stillNotInCatalog",
                                    "purpose": "must also be rejected",
                                }
                            ],
                        }
                    ],
                    "ambiguities": [],
                }
            )
        elif response_model is InitialPlanProposal:
            first_case = prompt["test_cases"][0]
            value = InitialPlanProposal.model_validate(
                {
                    "strategy": "execute one trusted contract test",
                    "selected": [
                        {
                            "kind": "test_case",
                            "item_id": first_case["item_id"],
                            "priority": 80,
                            "reason": "bounded smoke coverage",
                            "estimated_requests": 1,
                        }
                    ],
                }
            )
        elif response_model is AnalysisDecision:
            self.analysis_calls += 1
            if self.fail_analyzer:
                raise TimeoutError("provider timeout with unsafe detail")
            payload = (
                {
                    "decision": "add_diagnostic_case",
                    "reason": "probe one declared not-found response",
                    "hypothesis": {
                        "statement": "unknown resources may not map to 404",
                        "confidence": "medium",
                        "supporting_evidence": ["trajectory:actions/1"],
                    },
                    "diagnostic_case": {
                        "case_id": "unknown_user_probe",
                        "operation_id": "getUser",
                        "purpose": "probe the not-found branch once",
                        "expected_status_codes": [404],
                    },
                }
                if self.analysis_calls == 1
                else {
                    "decision": "continue_plan",
                    "reason": "the diagnostic result resolved the active hypothesis",
                    "hypothesis": {
                        "statement": "the diagnostic observation resolved the branch",
                        "confidence": "high",
                        "supporting_evidence": ["trajectory:actions/2"],
                    },
                }
            )
            value = AnalysisDecision.model_validate(payload)
        else:
            raise AssertionError(f"unexpected response model: {response_model}")
        return ModelResponse(
            value=value,
            telemetry=ModelCallTelemetry(calls=1, duration_ms=1),
        )


def test_pipeline_passes_in_memory_state_directly_to_runtime(monkeypatch, tmp_path):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    gateway = PipelineGateway()
    captured = {}

    def fake_run(state, plan, workflows, actual_catalog, config, *, checkpoint=None):
        captured["state"] = state
        captured["plan"] = plan
        assert actual_catalog is catalog
        assert workflows == []
        assert checkpoint is None
        return AgentRun(state=state.model_copy(update={"phase": "finished"}))

    monkeypatch.setattr("api_doctor.agent.pipeline.run_agent_actions", fake_run)
    result = AgentPipeline(gateway).run(
        catalog,
        [],
        RunConfig(
            base_url="http://localhost:8080",
            seed=7,
            time_anchor=datetime.fromisoformat("2026-10-02T10:00:00+00:00"),
            max_tests=2,
        ),
        max_steps=3,
    )

    assert gateway.calls == [
        "semantic-test-discovery",
        "workflow-discovery",
        "initial-planner",
    ]
    assert captured["state"] is result.planning.state
    assert captured["plan"] == result.test_plan
    assert result.run.state.phase == "finished"
    assert result.model_usage["total"].calls == 3
    assert result.workflow_validation.summary()["rejected"] == 1
    artifacts = AgentArtifactStore(tmp_path / "run").finalize_pipeline(result)
    pipeline = artifacts["pipeline"].read_text(encoding="utf-8")
    assert '"workflow_validation"' in pipeline
    assert '"initial_planner"' in pipeline


def test_pipeline_analyzes_failure_and_executes_validated_diagnostic_case(monkeypatch):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    gateway = PipelineGateway()
    calls = 0

    def fake_run(state, plan, workflows, actual_catalog, config, *, checkpoint=None):
        nonlocal calls
        del config, checkpoint
        calls += 1
        assert actual_catalog is catalog
        if calls == 1:
            action = state.pending_actions[0]
            observation = AgentObservation(
                action_kind=action.kind,
                action_id=action.item_id,
                outcome="failed",
                evidence_ref="trajectory:actions/1",
                summary="unexpected success response",
                request_count=1,
                violations=["status 201 is not expected"],
            )
            failed_state = state.model_copy(
                deep=True,
                update={
                    "phase": "analyzing",
                    "pending_actions": [],
                    "completed_actions": [action],
                    "observations": [observation],
                    "remaining_test_budget": state.remaining_test_budget - 1,
                    "remaining_step_budget": state.remaining_step_budget - 1,
                },
            )
            return AgentRun(
                state=failed_state,
                records=[AgentActionRecord(action=action, observation=observation)],
            )

        action = state.pending_actions[0]
        assert action.item_id == "DIAG-unknown_user_probe"
        assert action.origin == "diagnostic"
        assert any(
            item.case.case_id == "DIAG-unknown_user_probe" for item in plan.cases
        )
        assert all(item.workflow_id != "unknown_user_probe" for item in workflows)
        observation = AgentObservation(
            action_kind="test_case",
            action_id=action.item_id,
            outcome="passed",
            evidence_ref="trajectory:actions/2",
            summary="diagnostic returned 404 as expected",
            request_count=1,
        )
        return AgentRun(
            state=state.model_copy(
                deep=True,
                update={
                    "phase": "analyzing",
                    "pending_actions": [],
                    "completed_actions": [*state.completed_actions, action],
                    "observations": [*state.observations, observation],
                    "remaining_test_budget": state.remaining_test_budget - 1,
                    "remaining_step_budget": state.remaining_step_budget - 1,
                },
            ),
            records=[AgentActionRecord(action=action, observation=observation)],
        )

    monkeypatch.setattr("api_doctor.agent.pipeline.run_agent_actions", fake_run)
    result = AgentPipeline(gateway).run(
        catalog,
        [],
        RunConfig(
            base_url="http://localhost:8080",
            seed=7,
            time_anchor=datetime.fromisoformat("2026-10-02T10:00:00+00:00"),
            max_tests=4,
        ),
        max_steps=6,
    )

    assert calls == 2
    assert gateway.calls[-1] == "observation-analyzer"
    assert gateway.analysis_calls == 2
    assert result.run.analyses[0].applied_decision == "add_diagnostic_case"
    assert result.run.analyses[1].applied_decision == "continue_plan"
    assert result.run.state.plan_version == 2
    assert result.run.state.phase == "finished"
    assert result.model_usage["analyzer"].calls == 2


def test_pipeline_falls_back_when_analyzer_call_fails(monkeypatch):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    gateway = PipelineGateway(fail_analyzer=True)
    runtime_calls = 0

    def fake_run(state, plan, workflows, actual_catalog, config, *, checkpoint=None):
        nonlocal runtime_calls
        del plan, workflows, config, checkpoint
        runtime_calls += 1
        assert actual_catalog is catalog
        if runtime_calls == 1:
            action = state.pending_actions[0]
            pending = action.model_copy(update={"item_id": "remaining-case"})
            observation = AgentObservation(
                action_kind=action.kind,
                action_id=action.item_id,
                outcome="failed",
                evidence_ref="trajectory:actions/1",
                summary="trigger analyzer",
                request_count=1,
            )
            return AgentRun(
                state=state.model_copy(
                    deep=True,
                    update={
                        "phase": "analyzing",
                        "pending_actions": [pending],
                        "completed_actions": [action],
                        "observations": [observation],
                        "remaining_test_budget": state.remaining_test_budget - 1,
                        "remaining_step_budget": state.remaining_step_budget - 1,
                    },
                ),
                records=[AgentActionRecord(action=action, observation=observation)],
            )
        assert state.phase == "executing"
        assert state.pending_actions[0].item_id == "remaining-case"
        return AgentRun(
            state=state.model_copy(
                deep=True,
                update={
                    "phase": "finished",
                    "pending_actions": [],
                    "termination_reason": "fallback plan completed",
                },
            )
        )

    monkeypatch.setattr("api_doctor.agent.pipeline.run_agent_actions", fake_run)
    result = AgentPipeline(gateway).run(
        catalog,
        [],
        RunConfig(
            base_url="http://localhost:8080",
            seed=7,
            time_anchor=datetime.fromisoformat("2026-10-02T10:00:00+00:00"),
            max_tests=3,
        ),
        max_steps=5,
    )

    assert runtime_calls == 2
    assert result.run.state.termination_reason == "fallback plan completed"
    assert result.run.analyses[0].proposed is None
    assert result.run.analyses[0].applied_decision == "continue_plan"
    assert result.run.analyses[0].analyzer_error == "Analyzer unavailable (TimeoutError)"
    assert result.model_usage["analyzer"].calls == 0
