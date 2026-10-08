from datetime import datetime
from pathlib import Path

import pytest

from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi import load_openapi
from api_doctor.testspec.openapi_compiler import OpenAPICompileError, compile_openapi_candidate
from api_doctor.testspec.value_generator import ValueGenerationError, generate_value


ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime.fromisoformat("2026-10-02T10:00:00+00:00")


def _catalog_and_candidates():
    catalog = load_openapi(ROOT / "examples/buggy-shop/openapi.yaml")
    candidates = {item.rule_id: item for item in generate_schema_candidates(catalog)}
    return catalog, candidates


def test_compiles_happy_path_directly_from_operation_catalog():
    catalog, candidates = _catalog_and_candidates()
    case = compile_openapi_candidate(
        candidates["schema:createUser:happy-path"],
        catalog,
        case_number=1,
        seed=7,
        anchor=ANCHOR,
    )

    assert case.request.method == "POST"
    assert case.request.path_template == "/users"
    assert case.request.body["name"].startswith("test-name")
    assert case.request.body["email"].endswith("@example.test")
    assert case.assertions[0].allowed == [201]
    assert case.risk == "safe"


def test_all_current_buggy_shop_schema_candidates_compile():
    catalog, candidates = _catalog_and_candidates()
    compiled = [
        compile_openapi_candidate(
            candidate,
            catalog,
            case_number=index,
            seed=7,
            anchor=ANCHOR,
        )
        for index, candidate in enumerate(candidates.values(), start=1)
        if candidate.status == "compilable"
    ]

    assert len(compiled) == 25
    assert len({case.case_id for case in compiled}) == 25


def test_negative_candidate_has_valid_baseline_and_targeted_invalid_body():
    catalog, candidates = _catalog_and_candidates()
    case = compile_openapi_candidate(
        candidates["schema:createOrder:quantity-above-maximum"],
        catalog,
        case_number=2,
        seed=7,
        anchor=ANCHOR,
    )

    assert case.request.body["quantity"] == 101
    assert case.request.body["product_id"] >= 1
    assert case.assertions[0].allowed == [400, 422]


def test_compiles_not_found_parameter_probe():
    catalog, candidates = _catalog_and_candidates()
    case = compile_openapi_candidate(
        candidates["schema:getUser:path-id-not-found"],
        catalog,
        case_number=1,
        seed=7,
        anchor=ANCHOR,
    )

    assert case.request.path_parameters == {"id": 1_000_000_000}
    assert case.assertions[0].allowed == [404]


def test_compiler_materializes_required_parameters(tmp_path):
    spec = tmp_path / "openapi.yaml"
    spec.write_text("""
openapi: 3.1.0
info: {title: Parameters, version: '1'}
paths:
  /items/{item_id}:
    get:
      operationId: getItem
      parameters:
        - {name: item_id, in: path, required: true, schema: {type: integer, minimum: 1}}
        - {name: verbose, in: query, required: true, schema: {type: boolean}}
        - {name: X-Tenant, in: header, required: true, schema: {type: string}}
      responses: {'200': {description: ok}}
""", encoding="utf-8")
    catalog = load_openapi(spec)
    operation = catalog.get("getItem")

    from api_doctor.testspec.models import Expected, SemanticTestCandidate, Source, UseBaseline

    candidate = SemanticTestCandidate(
        rule_id="schema:getItem:happy-path",
        status="compilable",
        operation_id="getItem",
        purpose="valid parameters",
        mutations=[UseBaseline(kind="use_baseline")],
        expected=Expected(status_codes=[200]),
        source=Source(kind="openapi", id=f"{operation.source_pointer}/responses"),
    )
    case = compile_openapi_candidate(candidate, catalog, case_number=1, seed=5, anchor=ANCHOR)

    assert case.request.path_parameters == {"item_id": 1}
    assert case.request.query_parameters == {"verbose": True}
    assert case.request.headers["X-Tenant"].startswith("test-X-Tenant")
    assert case.request.body is None


def test_rejects_openapi_negative_mutation_that_does_not_break_schema():
    catalog, candidates = _catalog_and_candidates()
    candidate = candidates["schema:createUser:missing-email"].model_copy(deep=True)
    candidate.mutations[0].path = "$.name"
    catalog.get("createUser").request_body.json_schema["required"] = ["email"]

    with pytest.raises(OpenAPICompileError, match="did not violate"):
        compile_openapi_candidate(candidate, catalog, case_number=1, seed=7, anchor=ANCHOR)


def test_value_generator_handles_nested_objects_arrays_and_is_reproducible():
    schema = {
        "type": "object",
        "required": ["profile", "roles"],
        "properties": {
            "profile": {
                "type": "object",
                "required": ["email", "created_at"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
            "roles": {
                "type": "array",
                "minItems": 2,
                "uniqueItems": True,
                "items": {"type": "string"},
            },
        },
    }
    first = generate_value(schema, name="body", seed=11, anchor=ANCHOR)
    second = generate_value(schema, name="body", seed=11, anchor=ANCHOR)

    assert first == second
    assert first["profile"]["created_at"] == "2026-10-02T10:00:00Z"
    assert len(set(first["roles"])) == 2


def test_pattern_without_example_is_blocked_instead_of_guessed():
    with pytest.raises(ValueGenerationError, match="provide example or default"):
        generate_value(
            {"type": "string", "pattern": "^[A-Z]{3}-[0-9]{4}$"},
            name="code",
            seed=1,
            anchor=ANCHOR,
        )


def test_openapi_30_exclusive_bound_is_validated_with_30_semantics(tmp_path):
    spec = tmp_path / "openapi.yaml"
    spec.write_text("""
openapi: 3.0.3
info: {title: OAS30, version: '1'}
paths:
  /scores:
    post:
      operationId: createScore
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [score]
              properties:
                score: {type: integer, minimum: 0, exclusiveMinimum: true}
      responses:
        '201': {description: created}
        '400': {description: invalid}
""", encoding="utf-8")
    catalog = load_openapi(spec)
    candidates = {item.rule_id: item for item in generate_schema_candidates(catalog)}

    happy = compile_openapi_candidate(
        candidates["schema:createScore:happy-path"], catalog, case_number=1, seed=3, anchor=ANCHOR
    )
    invalid = compile_openapi_candidate(
        candidates["schema:createScore:score-at-exclusive-minimum"],
        catalog,
        case_number=2,
        seed=3,
        anchor=ANCHOR,
    )

    assert happy.request.body["score"] == 1
    assert invalid.request.body["score"] == 0
