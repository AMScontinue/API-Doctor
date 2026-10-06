"""Build one trusted test plan from mechanical and semantic candidates."""

from __future__ import annotations

from datetime import datetime

from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi.models import OperationCatalog
from api_doctor.testspec.fusion import CandidateIssue, FinalTestPlan, fuse_test_cases
from api_doctor.testspec.models import CompiledTestCase, RuleRecord, SemanticTestCandidate
from api_doctor.testspec.openapi_compiler import (
    OpenAPICompileError,
    compile_openapi_candidate,
)
from api_doctor.testspec.semantic_validation import validate_semantic_candidates


def build_final_test_plan(
    catalog: OperationCatalog,
    semantic_candidates: list[SemanticTestCandidate],
    rules: list[RuleRecord],
    *,
    seed: int,
    anchor: datetime,
) -> FinalTestPlan:
    """Compile, validate, and fuse both candidate sources."""
    compiled: list[CompiledTestCase] = []
    rejected: list[CandidateIssue] = []
    unsupported: list[CandidateIssue] = []

    for index, candidate in enumerate(generate_schema_candidates(catalog), start=1):
        if candidate.status == "not_compilable":
            unsupported.append(_issue(candidate, candidate.unsupported_reason))
            continue
        try:
            compiled.append(
                compile_openapi_candidate(
                    candidate,
                    catalog,
                    case_number=index,
                    seed=seed,
                    anchor=anchor,
                )
            )
        except OpenAPICompileError as error:
            rejected.append(_issue(candidate, str(error)))

    semantic = validate_semantic_candidates(
        semantic_candidates,
        catalog,
        rules,
        seed=seed,
        anchor=anchor,
    )
    for review in semantic.reviews:
        if review.verdict == "rejected":
            rejected.append(CandidateIssue(candidate=review.candidate, reasons=review.errors))
        elif review.compiled_case is not None:
            compiled.append(review.compiled_case)
        else:
            unsupported.append(_issue(review.candidate, review.candidate.unsupported_reason))

    return fuse_test_cases(
        compiled,
        rejected_candidates=rejected,
        unsupported_candidates=unsupported,
    )


def _issue(candidate: SemanticTestCandidate, reason: str | None) -> CandidateIssue:
    return CandidateIssue(
        candidate=candidate,
        reasons=[reason or "candidate is not compilable"],
    )
