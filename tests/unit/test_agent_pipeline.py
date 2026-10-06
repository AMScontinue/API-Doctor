from datetime import datetime
from pathlib import Path

from api_doctor.agent.models import AgentRun, InitialPlanProposal
from api_doctor.agent.persistence import AgentArtifactStore
from api_doctor.agent.pipeline import AgentPipeline
from api_doctor.execution.models import RunConfig
from api_doctor.generation.semantic_discovery import SemanticDiscovery
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelResponse
from api_doctor.openapi import load_openapi


ROOT = Path(__file__).resolve().parents[2]


class PipelineGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

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
    assert captured["plan"] is result.test_plan
    assert result.run.state.phase == "finished"
    assert result.model_usage["total"].calls == 3
    assert result.workflow_validation.summary()["rejected"] == 1
    artifacts = AgentArtifactStore(tmp_path / "run").finalize_pipeline(result)
    pipeline = artifacts["pipeline"].read_text(encoding="utf-8")
    assert '"workflow_validation"' in pipeline
    assert '"initial_planner"' in pipeline
