"""Application-level orchestration for one complete Agent run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from api_doctor.agent.analyzer import (
    analyze_observation,
    apply_analysis_decision,
    apply_analyzer_failure,
)
from api_doctor.agent.models import (
    AnalysisRecord,
    AgentRun,
    AgentState,
    InitialPlanningResult,
    InitialPlanProposal,
)
from api_doctor.agent.planning import initialize_agent_state, propose_initial_plan
from api_doctor.agent.runtime import CheckpointCallback, run_agent_actions
from api_doctor.execution.models import RunConfig
from api_doctor.generation.semantic_discovery import SemanticDiscovery, discover_semantic_tests
from api_doctor.generation.workflow_discovery import discover_workflows
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelGateway, ModelResponse
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.fusion import FinalTestPlan, PlannedCase, request_fingerprint
from api_doctor.testspec.models import RuleRecord
from api_doctor.testspec.plan_builder import build_final_test_plan
from api_doctor.workflows.validation import (
    CompiledWorkflow,
    WorkflowValidationResult,
    validate_workflows,
)


AnalysisCheckpointCallback = Callable[[AgentState, AnalysisRecord], None]


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
        analysis_checkpoint: AnalysisCheckpointCallback | None = None,
    ) -> AgentPipelineResult:
        semantic_response = discover_semantic_tests(catalog, rules, self.gateway)
        plan = self._build_test_plan(catalog, rules, semantic_response.value, config)
        workflow_response, workflow_validation = self._discover_workflows(catalog, rules)
        workflows = workflow_validation.compiled_workflows()
        planning_response, planning = self._create_initial_state(
            catalog, plan, workflows, config, max_steps
        )

        run, analyzer_usage = self._execute_agent_loop(
            planning.state,
            plan,
            workflows,
            catalog,
            config,
            checkpoint,
            analysis_checkpoint,
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
                "analyzer": analyzer_usage,
                "total": (
                    semantic_response.telemetry
                    + workflow_response.telemetry
                    + planning_response.telemetry
                    + analyzer_usage
                ),
            },
        )

    def _execute_agent_loop(
        self,
        state: AgentState,
        plan: FinalTestPlan,
        workflows: list[CompiledWorkflow],
        catalog: OperationCatalog,
        config: RunConfig,
        checkpoint: CheckpointCallback | None,
        analysis_checkpoint: AnalysisCheckpointCallback | None,
    ) -> tuple[AgentRun, ModelCallTelemetry]:
        records = []
        analyses: list[AnalysisRecord] = []
        usage = ModelCallTelemetry(calls=0, duration_ms=0)
        active_plan = plan.model_copy(deep=True)
        active_workflows = list(workflows)

        while True:
            segment = run_agent_actions(
                state,
                active_plan,
                active_workflows,
                catalog,
                config,
                checkpoint=checkpoint,
            )
            state = segment.state
            records.extend(segment.records)
            if state.phase != "analyzing":
                break
            if state.remaining_step_budget < 1:
                state.phase = "finished"
                state.termination_reason = "insufficient agent step budget for failure analysis"
                break

            try:
                response = analyze_observation(state, records[-1], catalog, self.gateway)
            except Exception as error:
                application = apply_analyzer_failure(state, error)
            else:
                usage += response.telemetry
                application = apply_analysis_decision(
                    state,
                    response.value,
                    catalog,
                    seed=config.seed,
                    anchor=config.time_anchor,
                    existing_case_ids={item.case.case_id for item in active_plan.cases},
                    existing_workflow_ids={item.workflow_id for item in active_workflows},
                )
            state = application.state
            analyses.append(application.record)
            if analysis_checkpoint is not None:
                analysis_checkpoint(state.model_copy(deep=True), application.record)
            if application.diagnostic_case is not None:
                active_plan.cases.append(
                    PlannedCase(
                        request_fingerprint=request_fingerprint(
                            application.diagnostic_case
                        ),
                        origins=[],
                        case=application.diagnostic_case,
                    )
                )
            if application.diagnostic_workflow is not None:
                active_workflows.append(application.diagnostic_workflow)
            if state.phase != "executing":
                break

        return AgentRun(state=state, records=records, analyses=analyses), usage

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
        rules: list[RuleRecord],
    ) -> tuple[ModelResponse[WorkflowDiscovery], WorkflowValidationResult]:
        response = discover_workflows(catalog, self.gateway, rules)
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
