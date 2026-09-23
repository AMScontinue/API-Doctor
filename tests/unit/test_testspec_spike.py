from datetime import datetime
from pathlib import Path

import pytest

from api_doctor.evaluation.runner import load_rules, run_spike
from api_doctor.generation.semantic_extractor import FixtureExtractor
from api_doctor.openapi.catalog import load_operations
from api_doctor.testspec.compiler import CompileError, compile_candidate


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "benchmarks/m0"
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _candidate(rule_id: str):
    extractor = FixtureExtractor(FIXTURES / "expected_candidates.json")
    rules = {item.id: item for item in load_rules(FIXTURES / "rules.yaml")}
    operations = load_operations(FIXTURES / "operations.json")
    return extractor.extract(rules[rule_id], operations), operations


def test_datetime_relation_compiles_reproducibly():
    candidate, operations = _candidate("order-time-range")
    case = compile_candidate(candidate, operations, case_number=1, seed=7, anchor=ANCHOR)
    assert case.body["start_time"] == "2026-10-02T10:00:00Z"
    assert case.body["end_time"] == "2026-10-02T09:00:00Z"


def test_numeric_relation_compiles_to_violation():
    candidate, operations = _candidate("discount-not-over-total")
    case = compile_candidate(candidate, operations, case_number=2, seed=7, anchor=ANCHOR)
    assert case.body["discount_amount"] > case.body["total_amount"]


def test_conditional_required_rule_compiles():
    candidate, operations = _candidate("card-token-required")
    case = compile_candidate(candidate, operations, case_number=3, seed=7, anchor=ANCHOR)
    assert case.body["payment_method"] == "card"
    assert "card_token" not in case.body


def test_unknown_field_is_rejected():
    candidate, operations = _candidate("invalid-field-reference")
    with pytest.raises(CompileError, match="unknown field path"):
        compile_candidate(candidate, operations, case_number=9, seed=7, anchor=ANCHOR)


def test_offline_spike_metrics_without_http():
    extractor = FixtureExtractor(FIXTURES / "expected_candidates.json")
    metrics, records = run_spike(
        rules_path=FIXTURES / "rules.yaml",
        operations_path=FIXTURES / "operations.json",
        extractor=extractor,
        base_url=None,
        seed=7,
        anchor=ANCHOR,
        mode="fixture",
    )
    assert metrics.total_rules == 10
    assert metrics.structured_successes == 10
    assert metrics.compilable_candidates == 6
    assert metrics.compiled_cases == 5
    assert metrics.rejected_rules == 4
    assert metrics.invalid_reference_count == 1
    assert len(records) == 10
