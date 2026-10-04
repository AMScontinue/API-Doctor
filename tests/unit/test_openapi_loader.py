import json

import pytest

from api_doctor.openapi import OpenAPIParseError, OpenAPIReferenceError, load_openapi
from api_doctor.openapi.resolver import LocalRefResolver


def _write_spec(tmp_path, content: str, name: str = "openapi.yaml"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_yaml_resolves_refs_and_normalizes_operations(tmp_path):
    path = _write_spec(tmp_path, """
openapi: 3.1.0
info: {title: Example, version: 1.0.0}
security: [{bearerAuth: []}]
paths:
  /users/{id}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: integer}}
      - {name: locale, in: query, schema: {type: string, default: en}}
    get:
      operationId: getUser
      parameters:
        - {name: locale, in: query, required: true, schema: {type: string}}
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/User'}
components:
  schemas:
    User:
      type: object
      required: [id]
      properties: {id: {type: integer}}
  securitySchemes:
    bearerAuth: {type: http, scheme: bearer, bearerFormat: JWT}
""")
    catalog = load_openapi(path)

    operation = catalog.get("getUser")
    assert catalog.openapi_version == "3.1.0"
    assert operation.method == "GET"
    assert [(item.name, item.required) for item in operation.parameters] == [("id", True), ("locale", True)]
    assert operation.responses[0].json_schema["required"] == ["id"]
    assert operation.security == [{"bearerAuth": []}]
    assert catalog.security_schemes["bearerAuth"].scheme == "bearer"
    assert operation.source_pointer == "#/paths/~1users~1{id}/get"


def test_loads_json_and_generates_stable_operation_id(tmp_path):
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "JSON", "version": "1"},
        "paths": {"/health": {"get": {"responses": {"204": {"description": "ok"}}}}},
    }
    path = _write_spec(tmp_path, json.dumps(spec), "openapi.json")
    first = load_openapi(path)
    second = load_openapi(path)
    assert list(first.operations) == ["generated_get_health"]
    assert first.fingerprint == second.fingerprint
    assert first.warnings[0].code == "generated_operation_id"


def test_duplicate_operation_id_is_rejected(tmp_path):
    path = _write_spec(tmp_path, """
openapi: 3.0.3
info: {title: Duplicate, version: '1'}
paths:
  /a: {get: {operationId: same, responses: {'200': {description: ok}}}}
  /b: {post: {operationId: same, responses: {'201': {description: ok}}}}
""")
    with pytest.raises(OpenAPIParseError, match="duplicate operationId"):
        load_openapi(path)


def test_missing_and_external_references_are_explicit_errors(tmp_path):
    missing = _write_spec(tmp_path, """
openapi: 3.1.0
info: {title: Missing, version: '1'}
paths:
  /x:
    get:
      responses:
        '200': {$ref: '#/components/responses/Absent'}
""")
    with pytest.raises(OpenAPIReferenceError, match="does not exist"):
        load_openapi(missing)

    resolver = LocalRefResolver({})
    with pytest.raises(OpenAPIReferenceError, match="external reference"):
        resolver.resolve({"$ref": "other.yaml#/Thing"})


def test_recursive_ref_is_retained_with_a_warning():
    document = {"components": {"schemas": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}}}
    resolver = LocalRefResolver(document)
    resolved = resolver.resolve({"$ref": "#/components/schemas/Node"})
    assert resolved["properties"]["child"] == {"$ref": "#/components/schemas/Node"}
    assert resolver.warnings[0].code == "recursive_ref"


def test_non_json_media_type_produces_location_aware_warning(tmp_path):
    path = _write_spec(tmp_path, """
openapi: 3.0.3
info: {title: Upload, version: '1'}
paths:
  /upload:
    post:
      operationId: upload
      requestBody:
        content:
          multipart/form-data: {schema: {type: object}}
      responses: {'204': {description: ok}}
""")
    catalog = load_openapi(path)
    warning = next(item for item in catalog.warnings if item.code == "non_json_media_type")
    assert warning.location.endswith("/requestBody/content")
