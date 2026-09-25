from datetime import datetime
import json
import os
from pathlib import Path

import pytest

from api_doctor.evaluation.runner import _matches_expectation, load_rules, run_spike
from api_doctor.config import load_dotenv
from api_doctor.generation.semantic_extractor import FixtureExtractor
from api_doctor.llm.deepseek import DeepSeekExtractor
from api_doctor.openapi.catalog import load_operations
from api_doctor.observability import redact
from api_doctor.testspec.compiler import CompileError, compile_candidate
from api_doctor.testspec.models import SemanticTestCandidate
from api_doctor.testspec.validation import CandidateContextError, validate_candidate_context


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "benchmarks/m0"
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _install_deepseek_responses(monkeypatch, candidates):
    responses = iter(candidates)
    sent_prompts = []

    class FakeResponse:
        def __init__(self, candidate):
            self._candidate = candidate

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            content = json.dumps(self._candidate)
            return json.dumps(
                {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                }
            ).encode()

    def fake_urlopen(request, timeout):
        assert timeout == 60
        payload = json.loads(request.data)
        sent_prompts.append(json.loads(payload["messages"][1]["content"]))
        return FakeResponse(next(responses))

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("api_doctor.llm.deepseek.urlopen", fake_urlopen)
    return sent_prompts


def _candidate(rule_id: str):
    extractor = FixtureExtractor(FIXTURES / "compiler_fixtures.json")
    rules = {item.id: item for item in load_rules(FIXTURES / "rules.yaml")}
    operations = load_operations(FIXTURES / "operations.json")
    return extractor.extract(rules[rule_id], operations).candidate, operations


def test_datetime_relation_compiles_reproducibly():
    candidate, operations = _candidate("order-time-range")
    case = compile_candidate(candidate, operations, case_number=1, seed=7, anchor=ANCHOR)
    assert case.body["start_time"] == "2026-10-02T10:00:00Z"
    assert case.body["end_time"] == "2026-10-02T09:00:00Z"


def test_numeric_relation_compiles_to_violation():
    candidate, operations = _candidate("discount-not-over-total")
    case = compile_candidate(candidate, operations, case_number=2, seed=7, anchor=ANCHOR)
    assert case.body["discount_amount"] > case.body["total_amount"]


def test_composed_cash_token_rule_compiles():
    candidate, operations = _candidate("cash-card-token-forbidden")
    case = compile_candidate(candidate, operations, case_number=3, seed=7, anchor=ANCHOR)
    assert case.body["payment_method"] == "cash"
    assert case.body["card_token"] == "unexpected-token"


def test_unknown_field_is_rejected():
    candidate, operations = _candidate("invalid-field-reference")
    with pytest.raises(CompileError, match="unknown field path"):
        compile_candidate(candidate, operations, case_number=9, seed=7, anchor=ANCHOR)


def test_offline_spike_metrics_without_http():
    extractor = FixtureExtractor(FIXTURES / "compiler_fixtures.json")
    metrics, records = run_spike(
        rules_path=FIXTURES / "rules.yaml",
        operations_path=FIXTURES / "operations.json",
        extractor=extractor,
        base_url=None,
        seed=7,
        anchor=ANCHOR,
        mode="fixture",
        expected_outcomes_path=FIXTURES / "expected_outcomes.json",
    )
    assert metrics.total_rules == 10
    assert metrics.structured_successes == 10
    assert metrics.compilable_candidates == 5
    assert metrics.compiled_cases == 5
    assert metrics.rejected_rules == 4
    assert metrics.invalid_reference_count == 1
    assert metrics.evaluation.correct_accepts == 5
    assert metrics.evaluation.correct_rejections == 4
    assert metrics.evaluation.false_accepts == 1
    assert len(records) == 10


def test_log_redaction_handles_sensitive_keys_and_testspec_paths():
    value = {
        "Authorization": "Bearer secret",
        "mutation": {"kind": "set_value", "path": "$.card_token", "value": "tok-secret"},
        "safe": "visible",
    }
    redacted = redact(value)
    assert redacted["Authorization"] == "***REDACTED***"
    assert redacted["mutation"]["value"] == "***REDACTED***"
    assert redacted["safe"] == "visible"


def test_dotenv_loader_does_not_override_existing_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("EXISTING=from-file\nNEW_VALUE='loaded'\n", encoding="utf-8")
    monkeypatch.setenv("EXISTING", "from-process")
    monkeypatch.delenv("NEW_VALUE", raising=False)
    loaded = load_dotenv(dotenv)
    assert os.environ["EXISTING"] == "from-process"
    assert os.environ["NEW_VALUE"] == "loaded"
    assert loaded == ["NEW_VALUE"]


def test_deepseek_returns_candidate_with_model_telemetry(monkeypatch):
    rules = {item.id: item for item in load_rules(FIXTURES / "rules.yaml")}
    operations = load_operations(FIXTURES / "operations.json")
    first = {
        "rule_id": "cash-card-token-forbidden",
        "status": "not_compilable",
        "operation_id": "createOrder",
        "purpose": "conditional required field",
        "mutations": [],
        "expected": {"status_codes": [422]},
        "source": {"kind": "business_rule", "id": "cash-card-token-forbidden"},
        "unsupported_reason": "conditional dependency is unsupported",
    }
    sent_prompts = _install_deepseek_responses(monkeypatch, [first])
    extractor = DeepSeekExtractor()
    result = extractor.extract(rules["cash-card-token-forbidden"], operations)

    assert result.candidate.status == "not_compilable"
    assert len(sent_prompts) == 1
    assert "several atomic mutations" in sent_prompts[0]["composition_rule"]
    assert result.telemetry is not None
    assert result.telemetry.calls == 1
    assert result.telemetry.prompt_tokens == 100
    assert result.telemetry.completion_tokens == 20


def test_deepseek_repairs_invalid_structure_once(monkeypatch):
    rules = {item.id: item for item in load_rules(FIXTURES / "rules.yaml")}
    operations = load_operations(FIXTURES / "operations.json")
    invalid = {
        "rule_id": "cash-card-token-forbidden",
        "status": "compilable",
        "operation_id": "createOrder",
        "purpose": "missing expected",
        "mutations": [{"kind": "set_value", "path": "$.payment_method", "value": "cash"}],
        "expected": None,
        "source": {"kind": "business_rule", "id": "cash-card-token-forbidden"},
        "unsupported_reason": None,
    }
    repaired = {
        **invalid,
        "status": "not_compilable",
        "mutations": [],
        "unsupported_reason": "cannot produce a valid evidence-backed candidate",
    }
    sent_prompts = _install_deepseek_responses(monkeypatch, [invalid, repaired])

    result = DeepSeekExtractor().extract(rules["cash-card-token-forbidden"], operations)

    assert result.candidate.status == "not_compilable"
    assert result.telemetry is not None
    assert result.telemetry.calls == 2
    assert result.telemetry.structure_repair_attempts == 1
    assert len(sent_prompts) == 2
    assert sent_prompts[1]["previous_invalid_output"]


def test_candidate_context_rejects_model_source_drift():
    rules = {item.id: item for item in load_rules(FIXTURES / "rules.yaml")}
    operations = load_operations(FIXTURES / "operations.json")
    candidate = SemanticTestCandidate.model_validate(
        {
            "rule_id": "cash-card-token-forbidden",
            "status": "compilable",
            "operation_id": "createOrder",
            "purpose": "drifted source",
            "mutations": [{"kind": "set_value", "path": "$.payment_method", "value": "cash"}],
            "expected": {"status_codes": [422]},
            "source": {"kind": "business_rule", "id": "another-rule"},
        }
    )
    with pytest.raises(CandidateContextError, match="source does not match"):
        validate_candidate_context(candidate, rules["cash-card-token-forbidden"], operations)


def test_candidate_context_rejects_expected_status_drift():
    candidate, operations = _candidate("cash-card-token-forbidden")
    rules = {item.id: item for item in load_rules(FIXTURES / "rules.yaml")}
    candidate.expected.status_codes = [400]
    with pytest.raises(CandidateContextError, match="expected status codes drifted"):
        validate_candidate_context(candidate, rules["cash-card-token-forbidden"], operations)


def test_heldout_benchmark_is_balanced_and_separate_from_prompt_examples():
    heldout = FIXTURES / "heldout"
    rules = load_rules(heldout / "rules.yaml")
    expectations = json.loads((heldout / "expected_outcomes.json").read_text())
    assert len(rules) == 16
    assert sum(item["status"] == "compilable" for item in expectations.values()) == 8
    assert sum(item["status"] == "not_compilable" for item in expectations.values()) == 8
    assert {item.id for item in rules} == set(expectations)


def test_semantic_expectation_requires_all_key_mutations():
    candidate, _ = _candidate("cash-card-token-forbidden")
    complete = {
        "status": "compilable",
        "required_mutations": [
            {"kind": "set_value", "path": "$.payment_method", "value": "cash"},
            {"kind": "set_value", "path": "$.card_token"},
        ],
    }
    missing = {
        "status": "compilable",
        "required_mutations": [{"kind": "omit_field", "path": "$.card_token"}],
    }
    assert _matches_expectation(candidate, complete)
    assert not _matches_expectation(candidate, missing)


def test_candidate_context_rejects_numeric_delta_for_datetime_relation():
    heldout = FIXTURES / "heldout"
    rules = {item.id: item for item in load_rules(heldout / "rules.yaml")}
    operations = load_operations(heldout / "operations.json")
    candidate = SemanticTestCandidate.model_validate(
        {
            "rule_id": "booking-time-order-paraphrase",
            "status": "compilable",
            "operation_id": "createBooking",
            "purpose": "invalid date delta",
            "mutations": [
                {
                    "kind": "field_relation",
                    "left": "$.finish_at",
                    "operator": "lt",
                    "right": "$.start_at",
                    "delta": {"value": 1, "unit": "number"},
                }
            ],
            "expected": {"status_codes": [422]},
            "source": {"kind": "business_rule", "id": "booking-time-order-paraphrase"},
        }
    )
    with pytest.raises(CandidateContextError, match="time delta unit"):
        validate_candidate_context(candidate, rules[candidate.rule_id], operations)


def test_not_compilable_candidate_may_clear_unknown_operation_hint():
    heldout = FIXTURES / "heldout"
    rules = {item.id: item for item in load_rules(heldout / "rules.yaml")}
    operations = load_operations(heldout / "operations.json")
    candidate = SemanticTestCandidate.model_validate(
        {
            "rule_id": "unknown-operation",
            "status": "not_compilable",
            "operation_id": None,
            "purpose": "operation does not exist",
            "mutations": [],
            "expected": None,
            "source": {"kind": "business_rule", "id": "unknown-operation"},
            "unsupported_reason": "approveInvoice is absent from the operation catalog",
        }
    )
    validate_candidate_context(candidate, rules[candidate.rule_id], operations)
