"""Compatibility entry point that compiles M0 candidates with the production compiler."""

from __future__ import annotations

from datetime import datetime

from api_doctor.evaluation.models import BenchmarkOperation
from api_doctor.evaluation.catalog import catalog_from_operations
from api_doctor.testspec.models import CompiledTestCase, SemanticTestCandidate
from api_doctor.testspec.openapi_compiler import OpenAPICompileError, compile_openapi_candidate


CompileError = OpenAPICompileError


def compile_candidate(
    candidate: SemanticTestCandidate,
    operations: dict[str, BenchmarkOperation],
    *,
    case_number: int,
    seed: int,
    anchor: datetime,
) -> CompiledTestCase:
    """Compile a benchmark candidate through the same path used by real OpenAPI input."""
    return compile_openapi_candidate(
        candidate,
        catalog_from_operations(operations),
        case_number=case_number,
        seed=seed,
        anchor=anchor,
    )
