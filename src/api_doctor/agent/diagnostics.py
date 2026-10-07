"""Compile one untrusted diagnostic request against trusted runtime state."""

from __future__ import annotations

from datetime import datetime

from api_doctor.agent.models import AgentVariable, DiagnosticCaseProposal
from api_doctor.openapi.models import OperationCatalog
from api_doctor.openapi.schema_validation import schema_errors
from api_doctor.testspec.models import (
    CompiledTestCase,
    Expected,
    SemanticTestCandidate,
    Source,
    UseBaseline,
)
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate
from api_doctor.workflows.validation import binding_target_schema


class DiagnosticCompileError(ValueError):
    """A model-proposed diagnostic cannot be grounded safely."""


def compile_diagnostic_case(
    proposal: DiagnosticCaseProposal,
    catalog: OperationCatalog,
    variables: dict[str, AgentVariable],
    *,
    seed: int,
    anchor: datetime,
) -> CompiledTestCase:
    operation = catalog.operations.get(proposal.operation_id)
    if operation is None:
        raise DiagnosticCompileError(f"unknown operation {proposal.operation_id!r}")

    declared = {
        int(item.status_code)
        for item in operation.responses
        if item.status_code.isdigit()
    }
    undeclared = set(proposal.expected_status_codes) - declared
    if undeclared:
        raise DiagnosticCompileError(
            f"diagnostic expects undeclared response status(es) {sorted(undeclared)}"
        )

    parameter_overrides: dict[tuple[str, str], object] = {}
    body_overrides: dict[str, object] = {}
    dependencies: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for binding in proposal.bindings:
        target = (binding.location, binding.name)
        if target in targets:
            raise DiagnosticCompileError(f"diagnostic binds {target!r} more than once")
        targets.add(target)
        schema = binding_target_schema(operation, *target)
        if schema is None:
            raise DiagnosticCompileError(
                f"unknown {binding.location} diagnostic target {binding.name!r}"
            )
        variable = variables.get(binding.variable)
        if variable is None:
            raise DiagnosticCompileError(
                f"unavailable or ambiguous working-memory variable {binding.variable!r}"
            )
        errors = schema_errors(schema, variable.value)
        if errors:
            raise DiagnosticCompileError(
                f"working-memory variable {binding.variable!r} violates target schema: "
                f"{errors[0].message}"
            )
        if binding.location == "body":
            body_overrides[binding.name] = variable.value
        else:
            parameter_overrides[target] = variable.value
        dependencies.add(variable.producer_case_id)

    for assignment in proposal.assignments:
        target = (assignment.location, assignment.name)
        if target in targets:
            raise DiagnosticCompileError(
                f"diagnostic assigns or binds {target!r} more than once"
            )
        targets.add(target)
        schema = binding_target_schema(operation, *target)
        if schema is None:
            raise DiagnosticCompileError(
                f"unknown {assignment.location} diagnostic target {assignment.name!r}"
            )
        errors = schema_errors(schema, assignment.value)
        if errors:
            raise DiagnosticCompileError(
                f"diagnostic constant for {target!r} violates target schema: "
                f"{errors[0].message}"
            )
        if assignment.location == "body":
            body_overrides[assignment.name] = assignment.value
        else:
            parameter_overrides[target] = assignment.value

    candidate = SemanticTestCandidate(
        rule_id=f"diagnostic:{proposal.case_id}",
        status="compilable",
        operation_id=operation.operation_id,
        purpose=proposal.purpose,
        mutations=[UseBaseline(kind="use_baseline")],
        expected=Expected(status_codes=sorted(set(proposal.expected_status_codes))),
        source=Source(kind="openapi", id=f"{operation.source_pointer}/responses"),
    )
    case = compile_openapi_candidate(
        candidate,
        catalog,
        case_number=1,
        seed=seed,
        anchor=anchor,
        parameter_overrides=parameter_overrides,
        body_overrides=body_overrides,
    )
    case.case_id = f"DIAG-{proposal.case_id}"
    case.dependencies = sorted(dependencies)
    return case
