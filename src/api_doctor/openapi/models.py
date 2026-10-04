"""Normalized, LLM-independent representation of an OpenAPI document."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OpenAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)


class ParseWarning(OpenAPIModel):
    code: str
    location: str
    message: str


class Parameter(OpenAPIModel):
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool = False
    description: str | None = None
    json_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")
    example: Any = None
    style: str | None = None
    explode: bool | None = None
    allow_reserved: bool = False


class RequestBody(OpenAPIModel):
    required: bool = False
    media_type: str
    json_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


class ResponseSpec(OpenAPIModel):
    status_code: str
    description: str = ""
    media_type: str | None = None
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class SecurityScheme(OpenAPIModel):
    name: str
    type: str
    scheme: str | None = None
    bearer_format: str | None = None
    parameter_name: str | None = None
    location: str | None = None
    flows: dict[str, Any] | None = None


class Operation(OpenAPIModel):
    operation_id: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
    path: str
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: RequestBody | None = None
    responses: list[ResponseSpec] = Field(default_factory=list)
    security: list[dict[str, list[str]]] | None = None
    source_pointer: str


class OperationCatalog(OpenAPIModel):
    title: str
    version: str
    openapi_version: str
    source: str
    fingerprint: str
    operations: dict[str, Operation]
    security_schemes: dict[str, SecurityScheme] = Field(default_factory=dict)
    warnings: list[ParseWarning] = Field(default_factory=list)

    def get(self, operation_id: str) -> Operation:
        return self.operations[operation_id]
