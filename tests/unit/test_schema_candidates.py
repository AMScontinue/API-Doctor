from datetime import datetime
from pathlib import Path

from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi import load_openapi
from api_doctor.testspec.compiler import compile_candidate
from api_doctor.testspec.models import (
    Boundary,
    OmitField,
    Operation as CompilerOperation,
    SetValue,
    UseBaseline,
)


ROOT = Path(__file__).resolve().parents[2]


def _by_id(candidates):
    return {item.rule_id: item for item in candidates}


def test_buggy_shop_schema_generates_deterministic_candidates():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    first = generate_schema_candidates(catalog)
    second = generate_schema_candidates(catalog)
    candidates = _by_id(first)

    assert [item.model_dump(mode="json") for item in first] == [
        item.model_dump(mode="json") for item in second
    ]
    assert isinstance(candidates["schema:createUser:happy-path"].mutations[0], UseBaseline)
    assert isinstance(candidates["schema:createUser:missing-email"].mutations[0], OmitField)
    assert isinstance(candidates["schema:createOrder:quantity-below-minimum"].mutations[0], Boundary)
    enum = candidates["schema:createOrder:payment_method-invalid-enum"]
    assert isinstance(enum.mutations[0], SetValue)
    assert enum.mutations[0].value not in {"cash", "card"}
    assert enum.expected.status_codes == [422]
    assert enum.source.kind == "openapi"


def test_use_baseline_candidate_is_accepted_by_existing_compiler():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    candidate = _by_id(generate_schema_candidates(catalog))["schema:createUser:happy-path"]
    operation = catalog.get("createUser")
    compiler_operation = CompilerOperation(
        operation_id=operation.operation_id,
        method=operation.method,
        path=operation.path,
        request_schema=operation.request_body.json_schema,
    )

    case = compile_candidate(
        candidate,
        {compiler_operation.operation_id: compiler_operation},
        case_number=1,
        seed=7,
        anchor=datetime.fromisoformat("2026-10-02T10:00:00+00:00"),
    )

    assert case.body == {"name": "test-name", "email": "email@example.test"}
    assert case.assertions[0].allowed == [201]


def test_missing_validation_response_keeps_candidate_but_refuses_compilation(tmp_path):
    spec = tmp_path / "openapi.yaml"
    spec.write_text("""
openapi: 3.1.0
info: {title: No validation response, version: '1'}
paths:
  /items:
    post:
      operationId: createItem
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [name]
              properties: {name: {type: string, minLength: 2}}
      responses: {'201': {description: created}}
""", encoding="utf-8")
    candidates = _by_id(generate_schema_candidates(load_openapi(spec)))

    assert candidates["schema:createItem:happy-path"].status == "compilable"
    missing = candidates["schema:createItem:missing-name"]
    assert missing.status == "not_compilable"
    assert "does not declare" in missing.unsupported_reason


def test_generates_string_array_and_exclusive_boundary_mutations(tmp_path):
    spec = tmp_path / "openapi.yaml"
    spec.write_text("""
openapi: 3.1.0
info: {title: Constraints, version: '1'}
paths:
  /items:
    post:
      operationId: createItem
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                score: {type: number, exclusiveMinimum: 0, exclusiveMaximum: 10}
                code: {type: string, minLength: 2, maxLength: 4}
                tags: {type: array, minItems: 1, maxItems: 2, uniqueItems: true}
      responses:
        '201': {description: created}
        '400': {description: invalid}
""", encoding="utf-8")
    candidates = _by_id(generate_schema_candidates(load_openapi(spec)))

    assert candidates["schema:createItem:score-at-exclusive-minimum"].mutations[0].value == 0
    assert candidates["schema:createItem:score-at-exclusive-maximum"].mutations[0].value == 10
    assert candidates["schema:createItem:code-below-min-length"].mutations[0].value == "x"
    assert candidates["schema:createItem:code-above-max-length"].mutations[0].value == "xxxxx"
    assert candidates["schema:createItem:tags-below-min-items"].mutations[0].value == []
    assert len(candidates["schema:createItem:tags-above-max-items"].mutations[0].value) == 3
    assert candidates["schema:createItem:tags-duplicate-items"].mutations[0].value == ["duplicate", "duplicate"]
