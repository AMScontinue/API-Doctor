"""Deterministically execute validated Agent actions and update working state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from api_doctor.agent.models import (
    AgentActionRecord,
    AgentObservation,
    AgentRun,
    AgentState,
    AgentVariable,
    PlannedAction,
)
from api_doctor.execution.http import execute_case
from api_doctor.execution.models import RunConfig, WorkflowInputVariable, WorkflowRun
from api_doctor.execution.safety import ExecutionPolicy
from api_doctor.execution.workflow_runner import run_workflow
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.fusion import FinalTestPlan, PlannedCase, request_fingerprint
from api_doctor.testspec.models import CompiledTestCase, TestResult
from api_doctor.workflows.validation import CompiledWorkflow


CheckpointCallback = Callable[[AgentState, AgentActionRecord], None]


class AgentRuntimeError(ValueError):
    """Agent state and its trusted registries cannot be executed safely."""


class _AgentRunner:
    def __init__(
        self,
        state: AgentState,
        plan: FinalTestPlan,
        workflows: list[CompiledWorkflow],
        catalog: OperationCatalog,
        config: RunConfig,
        checkpoint: CheckpointCallback | None,
    ) -> None:
        if state.spec_fingerprint != catalog.fingerprint:
            raise AgentRuntimeError("AgentState OpenAPI fingerprint does not match the catalog")
        self.state = state.model_copy(deep=True)
        self.catalog = catalog
        self.config = config
        self.checkpoint = checkpoint
        self.cases = {item.case.case_id: item for item in plan.cases}
        self.workflows = {item.workflow_id: item for item in workflows}
        self.records: list[AgentActionRecord] = []
        self.policy = ExecutionPolicy(
            base_url=config.base_url,
            approve_delete=config.approve_delete and state.delete_approved,
            timeout_seconds=config.timeout_seconds,
        )
        self.action_offset = len(state.observations)

    def run(self) -> AgentRun:
        while self.state.phase == "executing":
            if not self.state.pending_actions:
                self._finish("pending action queue exhausted")
                break
            action = self.state.pending_actions[0]
            trusted_cost = self._trusted_cost(action)
            if trusted_cost is None:
                record = self._registry_error(
                    action,
                    self.action_offset + len(self.records) + 1,
                    "trusted action registry mismatch",
                )
            elif action.estimated_requests != trusted_cost:
                record = self._registry_error(
                    action,
                    self.action_offset + len(self.records) + 1,
                    "action request cost does not match trusted registry",
                )
            elif trusted_cost > self.state.remaining_test_budget:
                self._finish("remaining request budget cannot execute the next action")
                break
            elif trusted_cost > self.state.remaining_step_budget:
                self._finish("remaining agent step budget cannot execute the next action")
                break
            else:
                record = self._execute(
                    action, self.action_offset + len(self.records) + 1
                )
            self.state.pending_actions.pop(0)
            self.state.completed_actions.append(action)
            self.state.observations.append(record.observation)
            self.state.remaining_test_budget -= record.observation.request_count
            self.state.remaining_step_budget -= record.observation.agent_steps
            self.state.executed_fingerprints.update(record.request_fingerprints)
            self.records.append(record)

            if record.workflow_run is not None:
                self._merge_variables(action.item_id, record.workflow_run)
            if self.state.termination_reason == "trusted action validation failed":
                self._finish(self.state.termination_reason)
            elif action.origin == "diagnostic":
                self.state.phase = "analyzing"
            elif record.observation.outcome in {"failed", "error"}:
                self.state.phase = "analyzing"
            elif not self.state.pending_actions:
                self._finish("pending action queue exhausted")
            elif self.state.remaining_test_budget == 0:
                self._finish("request budget exhausted")
            elif self.state.remaining_step_budget == 0:
                self._finish("agent step budget exhausted")

            if self.checkpoint is not None:
                self.checkpoint(self.state.model_copy(deep=True), record)

        return AgentRun(state=self.state, records=self.records)

    def _trusted_cost(self, action: PlannedAction) -> int | None:
        if action.kind == "test_case":
            return 1 if action.item_id in self.cases else None
        workflow = self.workflows.get(action.item_id)
        return len(workflow.steps) if workflow is not None else None

    def _execute(self, action: PlannedAction, action_number: int) -> AgentActionRecord:
        if action.kind == "test_case":
            planned = self.cases.get(action.item_id)
            if planned is None:
                return self._registry_error(
                    action, action_number, "trusted test case registry mismatch"
                )
            return self._execute_case(action, action_number, planned)
        workflow = self.workflows.get(action.item_id)
        if workflow is None:
            return self._registry_error(
                action, action_number, "trusted workflow registry mismatch"
            )
        return self._execute_workflow(action, action_number, workflow)

    def _execute_case(
        self,
        action: PlannedAction,
        action_number: int,
        planned: PlannedCase,
    ) -> AgentActionRecord:
        case = planned.case.model_copy(deep=True)
        fingerprint = planned.request_fingerprint
        if fingerprint in self.state.executed_fingerprints:
            result = TestResult(
                case_id=case.case_id,
                outcome="blocked",
                actual_status=None,
                expected_statuses=case.assertions[0].allowed,
                error="duplicate request fingerprint",
            )
            return self._case_record(action, action_number, case, result, [])

        result = execute_case(
            case,
            self.policy,
            operation=self.catalog.get(case.operation_id),
        )
        fingerprints = [fingerprint] if result.request_url is not None else []
        return self._case_record(action, action_number, case, result, fingerprints)

    def _case_record(
        self,
        action: PlannedAction,
        action_number: int,
        case: CompiledTestCase,
        result: TestResult,
        fingerprints: list[str],
    ) -> AgentActionRecord:
        details = result.violations or ([result.error] if result.error else [])
        summary = (
            f"{action.item_id} returned {result.actual_status}; outcome={result.outcome}; "
            f"expected={result.expected_statuses}"
        )
        return AgentActionRecord(
            action=action,
            observation=AgentObservation(
                action_kind=action.kind,
                action_id=action.item_id,
                outcome=result.outcome,
                evidence_ref=f"trajectory:actions/{action_number}",
                summary=summary,
                request_count=1 if result.request_url is not None else 0,
                agent_steps=1,
                violations=details,
            ),
            request_fingerprints=fingerprints,
            test_case=case,
            test_result=result,
        )

    def _execute_workflow(
        self,
        action: PlannedAction,
        action_number: int,
        workflow: CompiledWorkflow,
    ) -> AgentActionRecord:
        working_variables = self.state.unambiguous_variables()
        run = run_workflow(
            workflow,
            self.catalog,
            RunConfig(
                base_url=self.config.base_url,
                seed=self.config.seed,
                time_anchor=self.config.time_anchor,
                max_tests=min(action.estimated_requests, self.state.remaining_test_budget),
                approve_delete=self.config.approve_delete and self.state.delete_approved,
                timeout_seconds=self.config.timeout_seconds,
            ),
            seen_fingerprints=self.state.executed_fingerprints,
            input_variables=[
                WorkflowInputVariable(
                    name=variable.name,
                    value=variable.value,
                    producer_operation_id=variable.producer_operation_id,
                    producer_case_id=variable.producer_case_id,
                    response_expression=variable.response_expression,
                )
                for variable in working_variables.values()
            ],
        )
        results = [record.result for record in run.records if record.result is not None]
        request_count = sum(result.request_url is not None for result in results)
        processed_steps = sum(
            record.result is not None or record.state == "compile_error"
            for record in run.records
        )
        fingerprints = [
            request_fingerprint(record.compiled_case)
            for record in run.records
            if record.compiled_case is not None
            and record.result is not None
            and record.result.request_url is not None
        ]
        outcome = _workflow_outcome(run)
        violations = [
            detail
            for record in run.records
            for detail in [
                *(record.result.violations if record.result else []),
                *([record.reason] if record.reason else []),
            ]
        ]
        statuses = [result.actual_status for result in results if result.actual_status is not None]
        return AgentActionRecord(
            action=action,
            observation=AgentObservation(
                action_kind=action.kind,
                action_id=action.item_id,
                outcome=outcome,
                evidence_ref=f"trajectory:actions/{action_number}",
                summary=(
                    f"workflow {action.item_id} processed {processed_steps} step(s); "
                    f"statuses={statuses}; outcome={outcome}"
                ),
                request_count=request_count,
                agent_steps=max(processed_steps, 1),
                violations=violations,
            ),
            request_fingerprints=fingerprints,
            workflow_run=run,
        )

    def _registry_error(
        self,
        action: PlannedAction,
        action_number: int,
        reason: str,
    ) -> AgentActionRecord:
        self.state.termination_reason = "trusted action validation failed"
        return AgentActionRecord(
            action=action,
            observation=AgentObservation(
                action_kind=action.kind,
                action_id=action.item_id,
                outcome="error",
                evidence_ref=f"trajectory:actions/{action_number}",
                summary=f"cannot execute {action.kind} {action.item_id!r}: {reason}",
                agent_steps=1,
                violations=[reason],
            ),
        )

    def _merge_variables(self, action_id: str, run: WorkflowRun) -> None:
        self.state.variables.extend(
            AgentVariable(
                action_id=action_id,
                name=variable.name,
                value=variable.value,
                producer_operation_id=variable.producer_operation_id,
                producer_case_id=variable.producer_case_id,
                response_expression=variable.response_expression,
            )
            for variable in run.variables
        )

    def _finish(self, reason: str) -> None:
        self.state.phase = "finished"
        self.state.termination_reason = reason


def _workflow_outcome(
    run: WorkflowRun,
) -> Literal["passed", "failed", "error", "blocked"]:
    if run.metrics.errors:
        return "error"
    if run.metrics.failed:
        return "failed"
    if run.metrics.blocked:
        return "blocked"
    return "passed"


def run_agent_actions(
    state: AgentState,
    plan: FinalTestPlan,
    workflows: list[CompiledWorkflow],
    catalog: OperationCatalog,
    config: RunConfig,
    *,
    checkpoint: CheckpointCallback | None = None,
) -> AgentRun:
    """Consume pending actions until completion or an observation requires analysis."""
    return _AgentRunner(state, plan, workflows, catalog, config, checkpoint).run()
