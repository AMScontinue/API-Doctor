"""Typed contracts for semantic TestSpec compilation and execution."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


JsonValue = Any


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UseBaseline(StrictModel):
    kind: Literal["use_baseline"]


class SetValue(StrictModel):
    kind: Literal["set_value"]
    path: str = Field(pattern=r"^\$\.[A-Za-z_][A-Za-z0-9_]*$")
    value: JsonValue


class OmitField(StrictModel):
    kind: Literal["omit_field"]
    path: str = Field(pattern=r"^\$\.[A-Za-z_][A-Za-z0-9_]*$")


class Boundary(StrictModel):
    kind: Literal["boundary"]
    path: str = Field(pattern=r"^\$\.[A-Za-z_][A-Za-z0-9_]*$")
    position: Literal["below_minimum", "at_minimum", "above_maximum", "at_maximum"]


class Delta(StrictModel):
    value: int | float = Field(gt=0)
    unit: Literal["number", "second", "minute", "hour", "day"] = "number"


class FieldRelation(StrictModel):
    kind: Literal["field_relation"]
    left: str = Field(pattern=r"^\$\.[A-Za-z_][A-Za-z0-9_]*$")
    operator: Literal["lt", "lte", "eq", "ne", "gte", "gt"]
    right: str = Field(pattern=r"^\$\.[A-Za-z_][A-Za-z0-9_]*$")
    delta: Delta = Field(default_factory=lambda: Delta(value=1))


Mutation = Annotated[
    UseBaseline | SetValue | OmitField | Boundary | FieldRelation,
    Field(discriminator="kind"),
]


class Expected(StrictModel):
    status_codes: list[int] = Field(min_length=1)


class Source(StrictModel):
    kind: Literal["openapi", "business_rule", "heuristic"]
    id: str


class SemanticTestCandidate(StrictModel):
    rule_id: str
    status: Literal["compilable", "not_compilable"]
    operation_id: str | None = None
    purpose: str
    mutations: list[Mutation] = Field(default_factory=list)
    expected: Expected | None = None
    source: Source
    unsupported_reason: str | None = None

    @model_validator(mode="after")
    def check_status_payload(self) -> "SemanticTestCandidate":
        if self.status == "compilable":
            if not self.operation_id or not self.mutations or self.expected is None:
                raise ValueError("compilable candidate requires operation_id, mutations, and expected")
            if self.unsupported_reason:
                raise ValueError("compilable candidate cannot include unsupported_reason")
        elif not self.unsupported_reason:
            raise ValueError("not_compilable candidate requires unsupported_reason")
        return self


class Operation(StrictModel):
    operation_id: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    request_schema: dict[str, Any] = Field(default_factory=dict)


class Assertion(StrictModel):
    kind: Literal["status_in"] = "status_in"
    allowed: list[int]
    source: str
    evidence_ref: str


class CompiledRequest(StrictModel):
    method: str
    path_template: str
    path_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    query_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    headers: dict[str, JsonValue] = Field(default_factory=dict)
    cookies: dict[str, JsonValue] = Field(default_factory=dict)
    body: JsonValue = None
    media_type: str | None = None


class CompiledTestCase(StrictModel):
    case_id: str
    rule_id: str
    operation_id: str
    purpose: str
    request: CompiledRequest
    assertions: list[Assertion]
    dependencies: list[str] = Field(default_factory=list)
    risk: Literal["safe", "requires_approval"] = "safe"
    seed: int
    time_anchor: datetime

    @property
    def method(self) -> str:
        """Compatibility view for the M0 executor."""
        return self.request.method

    @property
    def path(self) -> str:
        """Compatibility view for the M0 executor."""
        return self.request.path_template

    @property
    def body(self) -> JsonValue:
        """Compatibility view for M0 tests and reports."""
        return self.request.body


class TestResult(StrictModel):
    case_id: str
    outcome: Literal["passed", "failed", "error", "blocked"]
    actual_status: int | None
    expected_statuses: list[int]
    response_body: JsonValue = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    request_url: str | None = None
    duration_ms: int | None = None
    violations: list[str] = Field(default_factory=list)
    error: str | None = None


class RuleRecord(StrictModel):
    id: str
    operation_id: str | None = None
    description: str
    expected_status_codes: list[int] | None = None
