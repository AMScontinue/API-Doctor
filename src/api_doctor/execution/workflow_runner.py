"""Execute one validated workflow through the restricted HTTP tool."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from api_doctor.execution.http import execute_case
from api_doctor.execution.models import (
    RunConfig,
    WorkflowRun,
    WorkflowRunMetrics,
    WorkflowStepRecord,
    WorkflowVariable,
)
from api_doctor.execution.safety import ExecutionPolicy
from api_doctor.execution.variables import VariableError, extract_response_value
from api_doctor.openapi.models import OperationCatalog
from api_doctor.openapi.schema_validation import schema_errors
from api_doctor.testspec.models import (
    Expected,
    SemanticTestCandidate,
    Source,
    TestResult,
    UseBaseline,
)
from api_doctor.testspec.fusion import request_fingerprint
from api_doctor.testspec.openapi_compiler import OpenAPICompileError, compile_openapi_candidate
from api_doctor.workflows.validation import (
    CompiledBinding,
    CompiledExtraction,
    CompiledWorkflow,
)


class WorkflowRuntimeError(ValueError):
    """A validated workflow cannot safely continue with its runtime observation."""


class _WorkflowRunner:
    def __init__(
        self,
        workflow: CompiledWorkflow,
        catalog: OperationCatalog,
        config: RunConfig,
        seen_fingerprints: set[str] | None = None,
    ) -> None:
        self.workflow = workflow
        self.catalog = catalog
        self.config = config
        self.policy = ExecutionPolicy(
            base_url=config.base_url,
            approve_delete=config.approve_delete,
            timeout_seconds=config.timeout_seconds,
        )
        self.variables: dict[str, WorkflowVariable] = {}
        self.records: list[WorkflowStepRecord] = []
        self.halt_reason: str | None = None
        self.seen_fingerprints = set(seen_fingerprints or ())
        self.workflow_fingerprints: set[str] = set()
        self.repeated_fingerprints: set[str] = set()

    def run(self) -> WorkflowRun:
        started = datetime.now(timezone.utc)
        for index, step in enumerate(self.workflow.steps, start=1):
            if self.halt_reason:
                self.records.append(
                    WorkflowStepRecord(step=step, state="blocked", reason=self.halt_reason)
                )
            elif index > self.config.max_tests:
                self.halt_reason = "max_tests budget exhausted"
                self.records.append(
                    WorkflowStepRecord(step=step, state="blocked", reason=self.halt_reason)
                )
            else:
                self.records.append(self._run_step(index))
        return WorkflowRun(
            run_id=str(uuid4()),
            workflow_id=self.workflow.workflow_id,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            spec_source=self.catalog.source,
            spec_fingerprint=self.catalog.fingerprint,
            base_url=self.config.base_url,
            seed=self.config.seed,
            time_anchor=self.config.time_anchor,
            max_tests=self.config.max_tests,
            metrics=self._metrics(),
            variables=list(self.variables.values()),
            records=self.records,
        )

    def _run_step(self, index: int) -> WorkflowStepRecord:
        step = self.workflow.steps[index - 1]
        try:
            parameter_overrides, body_overrides, dependencies = self._binding_values(
                step.bind
            )
            candidate = SemanticTestCandidate(
                rule_id=f"workflow:{self.workflow.workflow_id}:{step.step_id}",
                status="compilable",
                operation_id=step.operation_id,
                purpose=step.purpose,
                mutations=[UseBaseline(kind="use_baseline")],
                expected=Expected(status_codes=step.expected_status_codes),
                source=Source(
                    kind="openapi",
                    id=f"{self.catalog.get(step.operation_id).source_pointer}/responses",
                ),
            )
            case = compile_openapi_candidate(
                candidate,
                self.catalog,
                case_number=index,
                seed=self.config.seed,
                anchor=self.config.time_anchor,
                parameter_overrides=parameter_overrides,
                body_overrides=body_overrides,
            )
            case.case_id = f"WF-{self.workflow.workflow_id}-{step.step_id}"
            case.dependencies = sorted(dependencies)
        except (OpenAPICompileError, WorkflowRuntimeError) as error:
            self.halt_reason = f"workflow halted after compile failure: {error}"
            return WorkflowStepRecord(step=step, state="compile_error", reason=str(error))

        fingerprint = request_fingerprint(case)
        if fingerprint in self.seen_fingerprints:
            repeat_allowed = (
                step.repeat == "once"
                and fingerprint in self.workflow_fingerprints
                and fingerprint not in self.repeated_fingerprints
            )
            if not repeat_allowed:
                self.halt_reason = f"workflow halted before duplicate request {fingerprint}"
                return WorkflowStepRecord(
                    step=step,
                    state="blocked",
                    compiled_case=case,
                    result=TestResult(
                        case_id=case.case_id,
                        outcome="blocked",
                        actual_status=None,
                        expected_statuses=case.assertions[0].allowed,
                        error="duplicate request fingerprint",
                    ),
                    reason="duplicate request fingerprint",
                )
            self.repeated_fingerprints.add(fingerprint)

        result = execute_case(case, self.policy, operation=self.catalog.get(step.operation_id))
        if result.request_url is not None:
            self.seen_fingerprints.add(fingerprint)
            self.workflow_fingerprints.add(fingerprint)
        if result.outcome != "passed":
            self.halt_reason = (
                f"workflow halted after step {step.step_id!r} returned {result.outcome}"
            )
            return WorkflowStepRecord(
                step=step,
                state="blocked" if result.outcome == "blocked" else "executed",
                compiled_case=case,
                result=result,
                reason=result.error or (result.violations[0] if result.violations else None),
            )

        try:
            extracted = self._capture(step.step_id, step.operation_id, case.case_id, result, step.extract)
        except WorkflowRuntimeError as error:
            result.outcome = "error"
            result.error = str(error)
            self.halt_reason = f"workflow halted after variable extraction failure: {error}"
            return WorkflowStepRecord(
                step=step,
                state="executed",
                compiled_case=case,
                result=result,
                reason=str(error),
            )
        return WorkflowStepRecord(
            step=step,
            state="executed",
            compiled_case=case,
            result=result,
            extracted_variables=extracted,
        )

    def _binding_values(
        self,
        bindings: list[CompiledBinding],
    ) -> tuple[dict[tuple[str, str], object], dict[str, object], set[str]]:
        parameter_overrides: dict[tuple[str, str], object] = {}
        body_overrides: dict[str, object] = {}
        dependencies: set[str] = set()
        for binding in bindings:
            variable = self.variables.get(binding.variable)
            if variable is None:
                raise WorkflowRuntimeError(f"variable {binding.variable!r} is unavailable")
            errors = schema_errors(binding.target_schema, variable.value)
            if errors:
                raise WorkflowRuntimeError(
                    f"variable {binding.variable!r} violates target parameter schema: "
                    f"{errors[0].message}"
                )
            if binding.location == "body":
                body_overrides[binding.name] = variable.value
            else:
                parameter_overrides[(binding.location, binding.name)] = variable.value
            dependencies.add(variable.producer_case_id)
        return parameter_overrides, body_overrides, dependencies

    def _capture(
        self,
        step_id: str,
        operation_id: str,
        case_id: str,
        result: TestResult,
        extractions: list[CompiledExtraction],
    ) -> list[str]:
        if result.actual_status is None:
            raise WorkflowRuntimeError("successful result has no HTTP status")
        staged: dict[str, WorkflowVariable] = {}
        for extraction in extractions:
            source = next(
                (
                    item
                    for item in extraction.schemas
                    if item.status_code == result.actual_status
                ),
                None,
            )
            if source is None:
                raise WorkflowRuntimeError(
                    f"status {result.actual_status} does not support extraction "
                    f"{extraction.response_expression!r}"
                )
            try:
                value = extract_response_value(
                    result.response_body,
                    extraction.response_expression,
                )
            except VariableError as error:
                raise WorkflowRuntimeError(str(error)) from error
            errors = schema_errors(source.json_schema, value)
            if errors:
                raise WorkflowRuntimeError(
                    f"extracted variable {extraction.variable!r} violates its response schema: "
                    f"{errors[0].message}"
                )
            staged[extraction.variable] = WorkflowVariable(
                name=extraction.variable,
                value=value,
                producer_step_id=step_id,
                producer_operation_id=operation_id,
                producer_case_id=case_id,
                response_expression=extraction.response_expression,
                response_status=result.actual_status,
            )
        self.variables.update(staged)
        return list(staged)

    def _metrics(self) -> WorkflowRunMetrics:
        results = [record.result for record in self.records if record.result is not None]
        return WorkflowRunMetrics(
            total_steps=len(self.workflow.steps),
            executed=sum(record.state == "executed" for record in self.records),
            passed=sum(result.outcome == "passed" for result in results),
            failed=sum(result.outcome == "failed" for result in results),
            errors=sum(result.outcome == "error" for result in results)
            + sum(record.state == "compile_error" for record in self.records),
            blocked=sum(record.state == "blocked" for record in self.records),
        )


def run_workflow(
    workflow: CompiledWorkflow,
    catalog: OperationCatalog,
    config: RunConfig,
    *,
    seen_fingerprints: set[str] | None = None,
) -> WorkflowRun:
    return _WorkflowRunner(
        workflow,
        catalog,
        config,
        seen_fingerprints=seen_fingerprints,
    ).run()
