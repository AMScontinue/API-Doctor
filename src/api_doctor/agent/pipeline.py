"""Application-level orchestration for one complete Agent run."""

from __future__ import annotations

from dataclasses import dataclass

from api_doctor.agent.models import AgentRun, InitialPlanningResult, InitialPlanProposal
from api_doctor.agent.planning import initialize_agent_state, propose_initial_plan
from api_doctor.agent.runtime import CheckpointCallback, run_agent_actions
from api_doctor.execution.models import RunConfig
from api_doctor.generation.semantic_discovery import SemanticDiscovery, discover_semantic_tests
from api_doctor.generation.workflow_discovery import discover_workflows
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelGateway, ModelResponse
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.fusion import FinalTestPlan
from api_doctor.testspec.models import RuleRecord
from api_doctor.testspec.plan_builder import build_final_test_plan
from api_doctor.workflows.validation import (
    CompiledWorkflow,
    WorkflowValidationResult,
    validate_workflows,
)


@dataclass(frozen=True)
class AgentPipelineResult:
    """Typed products of discovery, planning, and execution."""

    semantic_discovery: SemanticDiscovery
    workflow_discovery: WorkflowDiscovery
    workflow_validation: WorkflowValidationResult
    test_plan: FinalTestPlan
    planning: InitialPlanningResult
    run: AgentRun
    model_usage: dict[str, ModelCallTelemetry]

    @property
    def workflows(self) -> list[CompiledWorkflow]:
        return self.workflow_validation.compiled_workflows()

class AgentPipeline:
    """Coordinate trusted stages without using files as in-process transport."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    def run(
        self,
        catalog: OperationCatalog,
        rules: list[RuleRecord],
        config: RunConfig,
        *,
        max_steps: int,
        checkpoint: CheckpointCallback | None = None,
    ) -> AgentPipelineResult:
        semantic_response = discover_semantic_tests(catalog, rules, self.gateway)
        plan = self._build_test_plan(catalog, rules, semantic_response.value, config)
        workflow_response, workflow_validation = self._discover_workflows(catalog)
        workflows = workflow_validation.compiled_workflows()
        planning_response, planning = self._create_initial_state(
            catalog, plan, workflows, config, max_steps
        )

        run = run_agent_actions(
            planning.state,
            plan,
            workflows,
            catalog,
            config,
            checkpoint=checkpoint,
        )

        return AgentPipelineResult(
            semantic_discovery=semantic_response.value,
            workflow_discovery=workflow_response.value,
            workflow_validation=workflow_validation,
            test_plan=plan,
            planning=planning,
            run=run,
            model_usage={
                "semantic_discovery": semantic_response.telemetry,
                "workflow_discovery": workflow_response.telemetry,
                "initial_planner": planning_response.telemetry,
                "total": (
                    semantic_response.telemetry
                    + workflow_response.telemetry
                    + planning_response.telemetry
                ),
            },
        )

    @staticmethod
    def _build_test_plan(
        catalog: OperationCatalog,
        rules: list[RuleRecord],
        discovery: SemanticDiscovery,
        config: RunConfig,
    ) -> FinalTestPlan:
        return build_final_test_plan(
            catalog,
            discovery.candidates,
            rules,
            seed=config.seed,
            anchor=config.time_anchor,
        )

    def _discover_workflows(
        self,
        catalog: OperationCatalog,
    ) -> tuple[ModelResponse[WorkflowDiscovery], WorkflowValidationResult]:
        response = discover_workflows(catalog, self.gateway)
        validated = validate_workflows(response.value.workflows, catalog)
        return response, validated

    def _create_initial_state(
        self,
        catalog: OperationCatalog,
        plan: FinalTestPlan,
        workflows: list[CompiledWorkflow],
        config: RunConfig,
        max_steps: int,
    ) -> tuple[ModelResponse[InitialPlanProposal], InitialPlanningResult]:
        response = propose_initial_plan(
            plan,
            workflows,
            max_tests=config.max_tests,
            max_steps=max_steps,
            delete_approved=config.approve_delete,
            gateway=self.gateway,
        )
        planning = initialize_agent_state(
            spec_fingerprint=catalog.fingerprint,
            plan=plan,
            workflows=workflows,
            proposed_plan=response.value,
            max_tests=config.max_tests,
            max_steps=max_steps,
            delete_approved=config.approve_delete,
        )
        return response, planning
