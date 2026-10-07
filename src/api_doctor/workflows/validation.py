"""Validate untrusted workflow proposals against an OperationCatalog."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import Field

from api_doctor.generation.workflow_models import WorkflowCandidate
from api_doctor.openapi.models import Operation, OperationCatalog, Parameter
from api_doctor.openapi.schema_validation import schema_errors
from api_doctor.testspec.models import StrictModel


class ExtractionSchema(StrictModel):
    status_code: int
    json_schema: dict[str, Any]


class CompiledExtraction(StrictModel):
    variable: str
    response_expression: str
    schemas: list[ExtractionSchema]


class CompiledBinding(StrictModel):
    location: Literal["path", "query", "header", "cookie", "body"]
    name: str
    variable: str
    source: Literal["workflow", "working_memory"] = "workflow"
    target_schema: dict[str, Any]


class CompiledAssignment(StrictModel):
    location: Literal["path", "query", "header", "cookie", "body"]
    name: str
    value: Any


class CompiledWorkflowStep(StrictModel):
    step_id: str
    operation_id: str
    method: str
    purpose: str
    expected_status_codes: list[int]
    repeat: Literal["forbid", "once"] = "forbid"
    extract: list[CompiledExtraction] = Field(default_factory=list)
    bind: list[CompiledBinding] = Field(default_factory=list)
    assign: list[CompiledAssignment] = Field(default_factory=list)
    requires_approval: bool = False


class CompiledWorkflow(StrictModel):
    workflow_id: str
    purpose: str
    confidence: Literal["low", "medium", "high"]
    steps: list[CompiledWorkflowStep]
    requires_approval: bool = False


class WorkflowReview(StrictModel):
    workflow: WorkflowCandidate
    verdict: Literal["accepted", "rejected"]
    errors: list[str] = Field(default_factory=list)
    compiled_workflow: CompiledWorkflow | None = None


class WorkflowValidationResult(StrictModel):
    reviews: list[WorkflowReview]

    def compiled_workflows(self) -> list[CompiledWorkflow]:
        """Return only proposals that passed deterministic validation."""
        return [
            review.compiled_workflow
            for review in self.reviews
            if review.compiled_workflow is not None
        ]

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.reviews),
            "accepted": sum(review.verdict == "accepted" for review in self.reviews),
            "rejected": sum(review.verdict == "rejected" for review in self.reviews),
            "compiled": sum(review.compiled_workflow is not None for review in self.reviews),
            "requiring_approval": sum(
                bool(review.compiled_workflow and review.compiled_workflow.requires_approval)
                for review in self.reviews
            ),
        }


def _decode_pointer(expression: str) -> list[str]:
    pointer = expression.removeprefix("$response.body#")
    if pointer == "":
        return []
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _combined_schema(kind: str, schemas: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not schemas:
        return None
    if len(schemas) == 1:
        return schemas[0]
    return {kind: schemas}


def _schema_child(schema: dict[str, Any], token: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    properties = schema.get("properties")
    if isinstance(properties, dict) and isinstance(properties.get(token), dict):
        matches.append(properties[token])
    if schema.get("type") == "array" and token.isdigit() and isinstance(schema.get("items"), dict):
        matches.append(schema["items"])

    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        children = [
            child
            for branch in branches
            if isinstance(branch, dict)
            if (child := _schema_child(branch, token)) is not None
        ]
        combined = _combined_schema(keyword, children)
        if combined is not None:
            matches.append(combined)
    return _combined_schema("allOf", matches)


def _schema_at_expression(
    schema: dict[str, Any],
    expression: str,
) -> dict[str, Any] | None:
    current = schema
    for token in _decode_pointer(expression):
        child = _schema_child(current, token)
        if child is None:
            return None
        current = child
    return current


def _json_types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            types = {
                item
                for branch in branches
                if isinstance(branch, dict)
                for item in _json_types(branch)
            }
            if types:
                return types
    values = schema.get("enum")
    if not isinstance(values, list) and "const" in schema:
        values = [schema["const"]]
    if isinstance(values, list):
        inferred = {_json_type(value) for value in values}
        return {item for item in inferred if item is not None}
    return set()


def _json_type(value: Any) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _types_compatible(source: dict[str, Any], target: dict[str, Any]) -> bool:
    source_types = _json_types(source)
    target_types = _json_types(target)
    if not source_types or not target_types:
        return False
    accepted = set(target_types)
    if "number" in accepted:
        accepted.add("integer")
    return source_types.issubset(accepted)


def _success_extractions(
    operation: Operation,
    expression: str,
) -> list[ExtractionSchema]:
    matches: list[ExtractionSchema] = []
    for response in operation.responses:
        if not response.status_code.isdigit() or int(response.status_code) // 100 != 2:
            continue
        if response.json_schema is None:
            continue
        extracted = _schema_at_expression(response.json_schema, expression)
        if extracted is not None:
            matches.append(
                ExtractionSchema(status_code=int(response.status_code), json_schema=extracted)
            )
    return matches


def _success_status_codes(operation: Operation) -> set[int]:
    return {
        int(response.status_code)
        for response in operation.responses
        if response.status_code.isdigit() and int(response.status_code) // 100 == 2
    }


def _declared_status_codes(operation: Operation) -> set[int]:
    return {
        int(response.status_code)
        for response in operation.responses
        if response.status_code.isdigit()
    }


def _parameter(operation: Operation, location: str, name: str) -> Parameter | None:
    return next(
        (
            parameter
            for parameter in operation.parameters
            if parameter.location == location and parameter.name == name
        ),
        None,
    )


def binding_target_schema(
    operation: Operation,
    location: str,
    name: str,
) -> dict[str, Any] | None:
    if location == "body":
        if operation.request_body is None:
            return None
        return _schema_child(operation.request_body.json_schema, name)
    parameter = _parameter(operation, location, name)
    return parameter.json_schema if parameter is not None else None


def _compile_workflow(
    workflow: WorkflowCandidate,
    catalog: OperationCatalog,
    working_memory: dict[str, object],
) -> tuple[CompiledWorkflow | None, list[str]]:
    errors = _workflow_structure_errors(workflow)
    if errors:
        return None, errors
    variables: dict[str, list[ExtractionSchema]] = {}
    compiled_steps: list[CompiledWorkflowStep] = []

    for step in workflow.steps:
        operation = catalog.operations.get(step.operation_id)
        if operation is None:
            errors.append(f"step {step.step_id!r} references unknown operation {step.operation_id!r}")
            continue

        extractions: list[CompiledExtraction] = []
        for extraction in step.extract:
            schemas = _success_extractions(operation, extraction.response_expression)
            if not schemas:
                errors.append(
                    f"step {step.step_id!r} extraction {extraction.response_expression!r} "
                    "is absent from every declared 2xx response schema"
                )
                continue
            variables[extraction.variable] = schemas
            extractions.append(
                CompiledExtraction(
                    variable=extraction.variable,
                    response_expression=extraction.response_expression,
                    schemas=schemas,
                )
            )

        status_sets = [
            {schema.status_code for schema in extraction.schemas}
            for extraction in extractions
        ]
        extraction_statuses = (
            set.intersection(*status_sets) if status_sets else _success_status_codes(operation)
        )
        expected_statuses = (
            set(step.expected_status_codes)
            if step.expected_status_codes is not None
            else extraction_statuses
        )
        undeclared = expected_statuses - _declared_status_codes(operation)
        if undeclared:
            errors.append(
                f"step {step.step_id!r} expects undeclared response status(es) "
                f"{sorted(undeclared)}"
            )
        if extractions and not expected_statuses.issubset(extraction_statuses):
            errors.append(
                f"step {step.step_id!r} cannot extract variables from expected "
                f"status(es) {sorted(expected_statuses - extraction_statuses)}"
            )
        if not expected_statuses:
            errors.append(
                f"step {step.step_id!r} has no exact declared 2xx response that satisfies "
                "all extractions"
            )

        bindings: list[CompiledBinding] = []
        occupied_targets: set[tuple[str, str]] = set()
        for binding in step.bind:
            target = (binding.location, binding.name)
            if target in occupied_targets:
                errors.append(f"step {step.step_id!r} binds {target!r} more than once")
                continue
            occupied_targets.add(target)
            target_schema = binding_target_schema(operation, *target)
            if target_schema is None:
                if binding.location == "body":
                    description = f"request body field {binding.name!r}"
                else:
                    description = f"{binding.location} parameter {binding.name!r}"
                errors.append(f"step {step.step_id!r} references unknown {description}")
                continue
            if binding.source == "working_memory":
                if binding.variable not in working_memory:
                    errors.append(
                        f"step {step.step_id!r} references unavailable working-memory "
                        f"variable {binding.variable!r}"
                    )
                    continue
                value_errors = schema_errors(target_schema, working_memory[binding.variable])
                if value_errors:
                    errors.append(
                        f"step {step.step_id!r} working-memory variable "
                        f"{binding.variable!r} violates target schema: "
                        f"{value_errors[0].message}"
                    )
                    continue
            else:
                source_schemas = variables.get(binding.variable, [])
                if not source_schemas:
                    errors.append(
                        f"step {step.step_id!r} variable {binding.variable!r} "
                        "has no valid extraction"
                    )
                    continue
                if not all(
                    _types_compatible(source.json_schema, target_schema)
                    for source in source_schemas
                ):
                    errors.append(
                        f"step {step.step_id!r} variable {binding.variable!r} is not "
                        f"type-compatible with {binding.location} parameter {binding.name!r}"
                    )
                    continue
            bindings.append(
                CompiledBinding(
                    location=binding.location,
                    name=binding.name,
                    variable=binding.variable,
                    source=binding.source,
                    target_schema=target_schema,
                )
            )

        assignments: list[CompiledAssignment] = []
        for assignment in step.assign:
            target = (assignment.location, assignment.name)
            if target in occupied_targets:
                errors.append(
                    f"step {step.step_id!r} assigns or binds {target!r} more than once"
                )
                continue
            occupied_targets.add(target)
            target_schema = binding_target_schema(operation, *target)
            if target_schema is None:
                if assignment.location == "body":
                    description = f"request body field {assignment.name!r}"
                else:
                    description = (
                        f"{assignment.location} parameter {assignment.name!r}"
                    )
                errors.append(f"step {step.step_id!r} references unknown {description}")
                continue
            value_errors = schema_errors(target_schema, assignment.value)
            if value_errors:
                errors.append(
                    f"step {step.step_id!r} constant for {target!r} violates target "
                    f"schema: {value_errors[0].message}"
                )
                continue
            assignments.append(
                CompiledAssignment(
                    location=assignment.location,
                    name=assignment.name,
                    value=assignment.value,
                )
            )

        compiled_steps.append(
            CompiledWorkflowStep(
                step_id=step.step_id,
                operation_id=operation.operation_id,
                method=operation.method,
                purpose=step.purpose,
                expected_status_codes=sorted(expected_statuses),
                repeat=step.repeat,
                extract=extractions,
                bind=bindings,
                assign=assignments,
                requires_approval=operation.method == "DELETE",
            )
        )

    if errors:
        return None, errors
    return (
        CompiledWorkflow(
            workflow_id=workflow.workflow_id,
            purpose=workflow.purpose,
            confidence=workflow.confidence,
            steps=compiled_steps,
            requires_approval=any(step.requires_approval for step in compiled_steps),
        ),
        [],
    )


def _workflow_structure_errors(workflow: WorkflowCandidate) -> list[str]:
    """Validate cross-step semantics independently for batch isolation."""
    errors: list[str] = []
    step_ids: set[str] = set()
    available_variables: set[str] = set()
    prior_operations: set[str] = set()
    for step in workflow.steps:
        if step.step_id in step_ids:
            errors.append(f"duplicate step_id {step.step_id!r}")
        step_ids.add(step.step_id)
        for binding in step.bind:
            if (
                binding.source == "workflow"
                and binding.variable not in available_variables
            ):
                errors.append(
                    f"step {step.step_id!r} variable {binding.variable!r} "
                    "is used before it is extracted"
                )
        for extraction in step.extract:
            if extraction.variable in available_variables:
                errors.append(f"duplicate variable {extraction.variable!r}")
            available_variables.add(extraction.variable)
        if step.repeat == "once":
            if step.operation_id not in prior_operations:
                errors.append(
                    f"step {step.step_id!r} cannot repeat operation "
                    f"{step.operation_id!r} before an earlier step uses it"
                )
            if step.expected_status_codes is None:
                errors.append(
                    f"step {step.step_id!r} must declare expected_status_codes "
                    "when repeat=once"
                )
        prior_operations.add(step.operation_id)
    return errors


def validate_workflows(
    workflows: list[WorkflowCandidate],
    catalog: OperationCatalog,
    *,
    working_memory: dict[str, object] | None = None,
) -> WorkflowValidationResult:
    """Review each workflow independently; proposals never bypass catalog checks."""
    reviews: list[WorkflowReview] = []
    id_counts = Counter(workflow.workflow_id for workflow in workflows)
    for workflow in workflows:
        compiled, errors = _compile_workflow(workflow, catalog, working_memory or {})
        if id_counts[workflow.workflow_id] > 1:
            errors.insert(0, f"duplicate workflow_id {workflow.workflow_id!r}")
            compiled = None
        reviews.append(
            WorkflowReview(
                workflow=workflow,
                verdict="rejected" if errors else "accepted",
                errors=errors,
                compiled_workflow=compiled,
            )
        )
    return WorkflowValidationResult(reviews=reviews)
