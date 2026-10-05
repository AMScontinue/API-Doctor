from datetime import datetime
from pathlib import Path

from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi import load_openapi
from api_doctor.testspec.fusion import CandidateIssue, fuse_test_cases
from api_doctor.testspec.models import Assertion, SemanticTestCandidate
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _compile(candidate: SemanticTestCandidate, number: int = 1):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    return compile_openapi_candidate(
        candidate,
        catalog,
        case_number=number,
        seed=7,
        anchor=ANCHOR,
    )


def _time_candidate(*, relation: bool, source_kind: str, source_id: str, rule_id: str):
    mutations = (
        [
            {
                "kind": "field_relation",
                "left": "$.end_time",
                "operator": "lte",
                "right": "$.start_time",
                "delta": {"value": 1, "unit": "hour"},
            }
        ]
        if relation
        else [
            {"kind": "set_value", "path": "$.start_time", "value": "2026-10-02T10:00:00Z"},
            {"kind": "set_value", "path": "$.end_time", "value": "2026-10-02T10:00:00Z"},
        ]
    )
    return SemanticTestCandidate.model_validate(
        {
            "rule_id": rule_id,
            "status": "compilable",
            "operation_id": "createOrder",
            "purpose": "reject an invalid time range",
            "mutations": mutations,
            "expected": {"status_codes": [422]},
            "source": {"kind": source_kind, "id": source_id},
        }
    )


def test_fuses_different_dsl_expressions_that_compile_to_same_request():
    business = _compile(
        _time_candidate(
            relation=True,
            source_kind="business_rule",
            source_id="order-time-range",
            rule_id="order-time-range",
        )
    )
    description = _compile(
        _time_candidate(
            relation=False,
            source_kind="openapi_description",
            source_id="#/paths/~1orders/post/description",
            rule_id="description-time-range",
        ),
        number=2,
    )

    plan = fuse_test_cases([business, description])

    assert plan.summary()["input_compiled"] == 2
    assert plan.summary()["executable"] == 1
    assert plan.summary()["deduplicated"] == 1
    assert [origin.rule_id for origin in plan.cases[0].origins] == [
        "description-time-range",
        "order-time-range",
    ]
    assert plan.cases[0].case.rule_id == "order-time-range"
    assert {item.source for item in plan.cases[0].case.assertions} == {
        "business_rule",
        "openapi_description",
    }


def test_quarantines_same_request_with_conflicting_expectations():
    original = _compile(
        _time_candidate(
            relation=True,
            source_kind="business_rule",
            source_id="order-time-range",
            rule_id="expects-422",
        )
    )
    conflicting = original.model_copy(
        deep=True,
        update={
            "rule_id": "expects-400",
            "assertions": [
                Assertion(
                    allowed=[400],
                    source="openapi_description",
                    evidence_ref="#/paths/~1orders/post/description",
                )
            ],
        },
    )

    plan = fuse_test_cases([original, conflicting])

    assert plan.summary()["executable"] == 0
    assert plan.summary()["conflicts"] == 1
    assert [variant.expected_status_codes for variant in plan.conflicts[0].variants] == [
        [400],
        [422],
    ]
    assert plan.conflicts[0].resolution == "requires_confirmation"
    assert plan.model_dump(mode="json") == fuse_test_cases(
        [conflicting, original]
    ).model_dump(mode="json")


def test_keeps_distinct_requests_and_is_stable_across_input_order():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    candidates = {
        candidate.rule_id: candidate for candidate in generate_schema_candidates(catalog)
    }
    first = compile_openapi_candidate(
        candidates["schema:createUser:happy-path"],
        catalog,
        case_number=7,
        seed=7,
        anchor=ANCHOR,
    )
    second = compile_openapi_candidate(
        candidates["schema:getUser:path-id-not-found"],
        catalog,
        case_number=2,
        seed=7,
        anchor=ANCHOR,
    )

    forward = fuse_test_cases([first, second])
    reverse = fuse_test_cases([second, first])

    assert forward.summary()["executable"] == 2
    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_preserves_rejected_and_unsupported_candidates_for_audit():
    candidate = _time_candidate(
        relation=True,
        source_kind="business_rule",
        source_id="order-time-range",
        rule_id="order-time-range",
    )
    rejected = CandidateIssue(candidate=candidate, reasons=["wrong evidence"])
    unsupported = CandidateIssue(candidate=candidate, reasons=["DSL cannot express state"])

    plan = fuse_test_cases(
        [],
        rejected_candidates=[rejected],
        unsupported_candidates=[unsupported],
    )

    assert plan.summary()["rejected"] == 1
    assert plan.summary()["unsupported"] == 1


def test_metadata_changes_do_not_prevent_deduplication_but_request_changes_do():
    original = _compile(
        _time_candidate(
            relation=True,
            source_kind="business_rule",
            source_id="order-time-range",
            rule_id="original",
        )
    )
    relabeled = original.model_copy(
        deep=True,
        update={"case_id": "untrusted-label", "rule_id": "relabeled", "purpose": "other prose"},
    )
    changed_request = original.model_copy(deep=True, update={"rule_id": "changed-request"})
    changed_request.request.query_parameters["dry_run"] = True

    plan = fuse_test_cases([original, relabeled, changed_request])

    assert plan.summary()["input_compiled"] == 3
    assert plan.summary()["executable"] == 2
    assert plan.summary()["deduplicated"] == 1
