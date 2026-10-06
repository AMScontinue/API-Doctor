"""Deterministically fuse compiled tests into an auditable execution plan."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable

from pydantic import Field

from api_doctor.testspec.models import (
    Assertion,
    CompiledRequest,
    CompiledTestCase,
    SemanticTestCandidate,
    StrictModel,
)


SOURCE_PRIORITY = {
    "openapi": 0,
    "business_rule": 1,
    "openapi_description": 2,
    "heuristic": 3,
}


class CandidateIssue(StrictModel):
    candidate: SemanticTestCandidate
    reasons: list[str] = Field(min_length=1)


class CandidateOrigin(StrictModel):
    case_id: str
    rule_id: str
    provenance: list[Assertion]


class PlannedCase(StrictModel):
    request_fingerprint: str
    origins: list[CandidateOrigin]
    case: CompiledTestCase


class ConflictVariant(StrictModel):
    expected_status_codes: list[int]
    origins: list[CandidateOrigin]


class PlanConflict(StrictModel):
    kind: str = "expected_status_conflict"
    request_fingerprint: str
    request: CompiledRequest
    variants: list[ConflictVariant]
    resolution: str = "requires_confirmation"


class FinalTestPlan(StrictModel):
    cases: list[PlannedCase]
    conflicts: list[PlanConflict]
    rejected_candidates: list[CandidateIssue] = Field(default_factory=list)
    unsupported_candidates: list[CandidateIssue] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "input_compiled": sum(len(case.origins) for case in self.cases)
            + sum(
                len(variant.origins)
                for conflict in self.conflicts
                for variant in conflict.variants
            ),
            "executable": len(self.cases),
            "deduplicated": sum(len(case.origins) - 1 for case in self.cases),
            "conflicts": len(self.conflicts),
            "rejected": len(self.rejected_candidates),
            "unsupported": len(self.unsupported_candidates),
        }


def request_fingerprint(case: CompiledTestCase) -> str:
    """Hash only observable request behavior, never labels or model prose."""
    payload = {
        "operation_id": case.operation_id,
        "request": case.request.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _expected_statuses(case: CompiledTestCase) -> tuple[int, ...]:
    return tuple(sorted({code for assertion in case.assertions for code in assertion.allowed}))


def _case_order(case: CompiledTestCase) -> tuple[int, str]:
    priority = min(
        (SOURCE_PRIORITY.get(assertion.source, len(SOURCE_PRIORITY)) for assertion in case.assertions),
        default=len(SOURCE_PRIORITY),
    )
    return priority, case.rule_id


def _assertions(cases: Iterable[CompiledTestCase]) -> list[Assertion]:
    unique: dict[tuple[str, str, tuple[int, ...]], Assertion] = {}
    for case in cases:
        for assertion in case.assertions:
            key = (assertion.source, assertion.evidence_ref, tuple(sorted(assertion.allowed)))
            unique[key] = assertion.model_copy(update={"allowed": list(key[2])})
    return [unique[key] for key in sorted(unique)]


def _origins(cases: Iterable[CompiledTestCase]) -> list[CandidateOrigin]:
    origins = [
        CandidateOrigin(
            case_id=case.case_id,
            rule_id=case.rule_id,
            provenance=_assertions([case]),
        )
        for case in cases
    ]
    return sorted(
        origins,
        key=lambda item: (
            item.rule_id,
            item.case_id,
            json.dumps(
                [source.model_dump(mode="json") for source in item.provenance],
                sort_keys=True,
            ),
        ),
    )


def _planned_case(
    fingerprint: str,
    cases: list[CompiledTestCase],
    plan_number: int,
) -> PlannedCase:
    ordered = sorted(cases, key=_case_order)
    canonical = ordered[0].model_copy(
        deep=True,
        update={
            "case_id": f"TC-PLAN-{plan_number:03d}",
            "assertions": _assertions(ordered),
        },
    )
    return PlannedCase(
        request_fingerprint=fingerprint,
        origins=_origins(cases),
        case=canonical,
    )


def _conflict(fingerprint: str, cases: list[CompiledTestCase]) -> PlanConflict:
    by_expectation: dict[tuple[int, ...], list[CompiledTestCase]] = defaultdict(list)
    for case in cases:
        by_expectation[_expected_statuses(case)].append(case)
    variants = [
        ConflictVariant(
            expected_status_codes=list(statuses),
            origins=_origins(variant_cases),
        )
        for statuses, variant_cases in sorted(by_expectation.items())
    ]
    return PlanConflict(
        request_fingerprint=fingerprint,
        request=sorted(cases, key=_case_order)[0].request,
        variants=variants,
    )


def fuse_test_cases(
    compiled_cases: Iterable[CompiledTestCase],
    *,
    rejected_candidates: Iterable[CandidateIssue] = (),
    unsupported_candidates: Iterable[CandidateIssue] = (),
) -> FinalTestPlan:
    """Deduplicate equivalent requests and quarantine expectation conflicts."""
    groups: dict[str, list[CompiledTestCase]] = defaultdict(list)
    for case in compiled_cases:
        groups[request_fingerprint(case)].append(case)

    planned: list[PlannedCase] = []
    conflicts: list[PlanConflict] = []
    for fingerprint in sorted(groups):
        cases = groups[fingerprint]
        if len({_expected_statuses(case) for case in cases}) > 1:
            conflicts.append(_conflict(fingerprint, cases))
        else:
            planned.append(_planned_case(fingerprint, cases, len(planned) + 1))

    return FinalTestPlan(
        cases=planned,
        conflicts=conflicts,
        rejected_candidates=list(rejected_candidates),
        unsupported_candidates=list(unsupported_candidates),
    )
