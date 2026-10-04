"""Fixed-plan deterministic OpenAPI baseline runner."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from api_doctor.execution.http import execute_openapi_case
from api_doctor.execution.models import BaselineRun, CaseRecord, RunMetrics
from api_doctor.execution.safety import ExecutionPolicy
from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.openapi_compiler import OpenAPICompileError, compile_openapi_candidate


def run_baseline(
    catalog: OperationCatalog,
    *,
    base_url: str,
    seed: int,
    anchor: datetime,
    max_tests: int,
    approve_delete: bool = False,
    timeout_seconds: float = 5.0,
) -> BaselineRun:
    if max_tests <= 0:
        raise ValueError("max_tests must be positive")
    started = datetime.now(timezone.utc)
    policy = ExecutionPolicy(
        base_url=base_url,
        approve_delete=approve_delete,
        timeout_seconds=timeout_seconds,
    )
    candidates = generate_schema_candidates(catalog)
    records: list[CaseRecord] = []
    compiled_count = executed_count = 0

    for index, candidate in enumerate(candidates, start=1):
        if candidate.status != "compilable":
            records.append(
                CaseRecord(
                    candidate=candidate,
                    state="blocked",
                    reason=candidate.unsupported_reason,
                )
            )
            continue
        if executed_count >= max_tests:
            records.append(
                CaseRecord(candidate=candidate, state="blocked", reason="max_tests budget exhausted")
            )
            continue
        try:
            case = compile_openapi_candidate(
                candidate,
                catalog,
                case_number=index,
                seed=seed,
                anchor=anchor,
            )
        except OpenAPICompileError as error:
            records.append(
                CaseRecord(candidate=candidate, state="compile_error", reason=str(error))
            )
            continue
        compiled_count += 1
        operation = catalog.get(case.operation_id)
        result = execute_openapi_case(case, operation, policy)
        if result.outcome == "blocked":
            records.append(
                CaseRecord(
                    candidate=candidate,
                    state="blocked",
                    compiled_case=case,
                    result=result,
                    reason=result.error,
                )
            )
            continue
        executed_count += 1
        records.append(
            CaseRecord(
                candidate=candidate,
                state="executed",
                compiled_case=case,
                result=result,
            )
        )

    results = [record.result for record in records if record.result is not None]
    metrics = RunMetrics(
        operations=len(catalog.operations),
        candidates=len(candidates),
        compilable_candidates=sum(item.status == "compilable" for item in candidates),
        compiled=compiled_count,
        executed=executed_count,
        passed=sum(item.outcome == "passed" for item in results),
        failed=sum(item.outcome == "failed" for item in results),
        errors=sum(item.outcome == "error" for item in results)
        + sum(record.state == "compile_error" for record in records),
        blocked=sum(record.state == "blocked" for record in records),
    )
    return BaselineRun(
        run_id=str(uuid4()),
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        spec_source=catalog.source,
        spec_fingerprint=catalog.fingerprint,
        base_url=base_url,
        seed=seed,
        time_anchor=anchor,
        max_tests=max_tests,
        metrics=metrics,
        records=records,
    )
