from datetime import datetime
from pathlib import Path

from api_doctor.generation.rules import load_business_rules
from api_doctor.openapi import load_openapi
from api_doctor.testspec.models import SemanticTestCandidate
from api_doctor.testspec.semantic_validation import validate_semantic_candidates


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _candidate(**overrides) -> SemanticTestCandidate:
    payload = {
        "rule_id": "order-time-range",
        "status": "compilable",
        "operation_id": "createOrder",
        "purpose": "reject an order whose end time is not later than its start time",
        "mutations": [
            {
                "kind": "field_relation",
                "left": "$.end_time",
                "operator": "lte",
                "right": "$.start_time",
                "delta": {"value": 1, "unit": "hour"},
            }
        ],
        "expected": {"status_codes": [422]},
        "source": {"kind": "business_rule", "id": "order-time-range"},
    }
    payload.update(overrides)
    return SemanticTestCandidate.model_validate(payload)


def _validate(candidates: list[SemanticTestCandidate]):
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    rules = load_business_rules(ROOT / "examples/buggy-shop/rules.yaml")
    return validate_semantic_candidates(candidates, catalog, rules, seed=7, anchor=ANCHOR)


def test_accepts_and_compiles_candidate_grounded_in_business_rule():
    review = _validate([_candidate()]).reviews[0]

    assert review.verdict == "accepted"
    assert review.errors == []
    assert review.compiled_case is not None
    assert (
        review.compiled_case.request.body["end_time"]
        <= review.compiled_case.request.body["start_time"]
    )


def test_rejects_rule_drift_without_aborting_other_candidates():
    invalid = _candidate(expected={"status_codes": [400]})
    result = _validate([invalid, _candidate()])

    assert result.summary() == {
        "total": 2,
        "accepted": 1,
        "rejected": 1,
        "compiled": 1,
        "accepted_not_compilable": 0,
    }
    assert "expected statuses drifted" in result.reviews[0].errors[0]


def test_rejects_unknown_fields_and_mechanical_required_field_tests():
    unknown = _candidate(mutations=[{"kind": "set_value", "path": "$.missing", "value": 1}])
    required = _candidate(mutations=[{"kind": "omit_field", "path": "$.product_id"}])
    result = _validate([unknown, required])

    assert result.summary()["rejected"] == 2
    assert "unknown top-level request field" in result.reviews[0].errors[0]
    assert "duplicates a mechanical test" in result.reviews[1].errors[0]


def test_accepts_schema_valid_candidate_grounded_in_openapi_description():
    pointer = "#/paths/~1orders/post/description"
    candidate = _candidate(
        rule_id="openapi-description-createOrder",
        mutations=[
            {
                "kind": "field_relation",
                "left": "$.end_time",
                "operator": "eq",
                "right": "$.start_time",
                "delta": {"value": 1, "unit": "hour"},
            }
        ],
        source={"kind": "openapi_description", "id": pointer},
    )
    review = _validate([candidate]).reviews[0]

    assert review.verdict == "accepted"
    assert review.compiled_case is not None


def test_rejects_description_candidate_with_wrong_evidence_pointer():
    candidate = _candidate(
        rule_id="openapi-description-createOrder",
        source={"kind": "openapi_description", "id": "#/paths/~1users/post/description"},
    )
    review = _validate([candidate]).reviews[0]

    assert review.verdict == "rejected"
    assert "does not cite" in review.errors[0]


def test_accepts_grounded_but_not_compilable_description_candidate():
    pointer = "#/paths/~1users~1{id}/get/description"
    candidate = SemanticTestCandidate.model_validate(
        {
            "rule_id": "openapi-description-getUser",
            "status": "not_compilable",
            "operation_id": "getUser",
            "purpose": "probe a missing user",
            "source": {"kind": "openapi_description", "id": pointer},
            "unsupported_reason": "the semantic DSL cannot express resource nonexistence",
        }
    )
    result = _validate([candidate])

    assert result.summary()["accepted_not_compilable"] == 1
    assert result.reviews[0].compiled_case is None


def test_rejects_description_candidate_without_operation():
    candidate = SemanticTestCandidate.model_validate(
        {
            "rule_id": "unbound-description",
            "status": "not_compilable",
            "purpose": "unsupported description constraint",
            "source": {
                "kind": "openapi_description",
                "id": "#/paths/~1orders/post/description",
            },
            "unsupported_reason": "not expressible",
        }
    )
    review = _validate([candidate]).reviews[0]

    assert review.verdict == "rejected"
    assert "requires a known operation_id" in review.errors[0]
