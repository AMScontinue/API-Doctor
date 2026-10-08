"""Fixed-plan deterministic OpenAPI baseline runner."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from api_doctor.execution.http import execute_case
from api_doctor.execution.models import BaselineRun, CaseRecord, RunConfig, RunMetrics
from api_doctor.execution.safety import ExecutionPolicy
from api_doctor.execution.variables import LinkBindings, VariableError
from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.models import SemanticTestCandidate
from api_doctor.testspec.openapi_compiler import OpenAPICompileError, compile_openapi_candidate


class _BaselineRunner:
    """Own mutable state for one complete deterministic run."""

    def __init__(self, catalog: OperationCatalog, config: RunConfig) -> None:
        self.catalog = catalog
        self.config = config
        self.policy = ExecutionPolicy(
            base_url=config.base_url,
            approve_delete=config.approve_delete,
            timeout_seconds=config.timeout_seconds,
        )
        self.bindings = LinkBindings(catalog)
        generated = generate_schema_candidates(catalog, allow_linked_setup=True)
        self.candidates = self.bindings.order_candidates(generated)
        self.records: list[CaseRecord] = []
        self.compiled_count = 0
        self.executed_count = 0

    def run(self) -> BaselineRun:
        started = datetime.now(timezone.utc)
        for index, candidate in enumerate(self.candidates, start=1):
            self.records.append(self._run_candidate(index, candidate))
        return BaselineRun(
            run_id=str(uuid4()),
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            spec_source=self.catalog.source,
            spec_fingerprint=self.catalog.fingerprint,
            base_url=self.config.base_url,
            seed=self.config.seed,
            time_anchor=self.config.time_anchor,
            max_tests=self.config.max_tests,
            metrics=self._metrics(),
            variables=self.bindings.variables,
            records=self.records,
        )

    def _run_candidate(
        self, index: int, candidate: SemanticTestCandidate
    ) -> CaseRecord:
        # Rejected candidates remain visible in the report instead of disappearing.
        if candidate.status != "compilable":
            return self._blocked(candidate, candidate.unsupported_reason)
        if self.executed_count >= self.config.max_tests:
            return self._blocked(candidate, "max_tests budget exhausted")

        operation_id = candidate.operation_id or ""
        if candidate.is_baseline:
            missing = self.bindings.missing_for(operation_id)
            if missing:
                return self._blocked(candidate, f"missing linked variables: {missing}")
            overrides = self.bindings.overrides_for(operation_id)
        else:
            overrides = {}

        try:
            # Compilation is the trust boundary between candidates and HTTP requests.
            case = compile_openapi_candidate(
                candidate,
                self.catalog,
                case_number=index,
                seed=self.config.seed,
                anchor=self.config.time_anchor,
                parameter_overrides=overrides,
            )
        except OpenAPICompileError as error:
            return CaseRecord(candidate=candidate, state="compile_error", reason=str(error))

        self.compiled_count += 1
        result = execute_case(
            case,
            self.policy,
            operation=self.catalog.get(case.operation_id),
        )
        if result.outcome == "blocked":
            return CaseRecord(
                candidate=candidate,
                state="blocked",
                compiled_case=case,
                result=result,
                reason=result.error,
            )

        self.executed_count += 1
        if result.outcome == "passed":
            try:
                self.bindings.capture(case, result)
            except VariableError as error:
                result.outcome = "error"
                result.error = str(error)
        return CaseRecord(
            candidate=candidate,
            state="executed",
            compiled_case=case,
            result=result,
        )

    @staticmethod
    def _blocked(candidate: SemanticTestCandidate, reason: str | None) -> CaseRecord:
        return CaseRecord(candidate=candidate, state="blocked", reason=reason)

    def _metrics(self) -> RunMetrics:
        results = [record.result for record in self.records if record.result is not None]
        return RunMetrics(
            operations=len(self.catalog.operations),
            candidates=len(self.candidates),
            compilable_candidates=sum(
                candidate.status == "compilable" for candidate in self.candidates
            ),
            compiled=self.compiled_count,
            executed=self.executed_count,
            passed=sum(result.outcome == "passed" for result in results),
            failed=sum(result.outcome == "failed" for result in results),
            errors=sum(result.outcome == "error" for result in results)
            + sum(record.state == "compile_error" for record in self.records),
            blocked=sum(record.state == "blocked" for record in self.records),
        )


def run_baseline(catalog: OperationCatalog, config: RunConfig) -> BaselineRun:
    """Run OpenAPI parsing output through generation, compilation, HTTP, and reporting data."""
    return _BaselineRunner(catalog, config).run()
