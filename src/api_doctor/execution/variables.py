"""OpenAPI Link evaluation and auditable working-variable storage."""

from __future__ import annotations

from typing import Any

from api_doctor.execution.models import WorkingVariable
from api_doctor.openapi.models import Operation, OperationCatalog, ResponseSpec
from api_doctor.testspec.models import CompiledTestCase, SemanticTestCandidate, TestResult


class VariableError(ValueError):
    """An OpenAPI Link cannot be evaluated deterministically."""


def _response(operation: Operation, status: int) -> ResponseSpec | None:
    for key in (str(status), f"{status // 100}XX", "default"):
        value = next((item for item in operation.responses if item.status_code == key), None)
        if value:
            return value
    return None


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise VariableError(f"invalid JSON Pointer {pointer!r}")
    current = value
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VariableError(f"response pointer {pointer!r} does not exist") from exc
    return current


def _extract(body: Any, expression: str) -> Any:
    prefix = "$response.body#"
    if not expression.startswith(prefix):
        raise VariableError(f"unsupported Link runtime expression {expression!r}")
    return _json_pointer(body, expression[len(prefix):])


def _inbound_link_parameters(
    catalog: OperationCatalog,
) -> dict[str, set[tuple[str, str]]]:
    inbound: dict[str, set[tuple[str, str]]] = {}
    for operation in catalog.operations.values():
        for response in operation.responses:
            for link in response.links.values():
                consumer = catalog.operations.get(link.operation_id)
                if consumer is None:
                    continue
                for name in link.parameters:
                    matches = [item for item in consumer.parameters if item.name == name]
                    if len(matches) == 1:
                        inbound.setdefault(consumer.operation_id, set()).add(
                            (matches[0].location, name)
                        )
    return inbound


class LinkBindings:
    """Own the runtime values required by explicit OpenAPI Links."""

    def __init__(self, catalog: OperationCatalog) -> None:
        self._catalog = catalog
        self._required = _inbound_link_parameters(catalog)
        self._values: dict[tuple[str, str, str], WorkingVariable] = {}

    @property
    def variables(self) -> list[WorkingVariable]:
        return list(self._values.values())

    def order_candidates(
        self, candidates: list[SemanticTestCandidate]
    ) -> list[SemanticTestCandidate]:
        """Place Link producers before consumers without depending on YAML path order."""
        operation_ids = list(self._catalog.operations)
        dependencies = {operation_id: set() for operation_id in operation_ids}
        for producer in self._catalog.operations.values():
            for response in producer.responses:
                for link in response.links.values():
                    if link.operation_id in dependencies and link.operation_id != producer.operation_id:
                        dependencies[link.operation_id].add(producer.operation_id)

        ordered: list[str] = []
        remaining = set(operation_ids)
        while remaining:
            ready = [
                operation_id
                for operation_id in operation_ids
                if operation_id in remaining and not (dependencies[operation_id] & remaining)
            ]
            if not ready:
                ready = [operation_id for operation_id in operation_ids if operation_id in remaining]
            next_operation = ready[0]
            ordered.append(next_operation)
            remaining.remove(next_operation)
        rank = {operation_id: index for index, operation_id in enumerate(ordered)}
        return sorted(
            candidates,
            key=lambda candidate: rank.get(candidate.operation_id or "", len(rank)),
        )

    def missing_for(self, operation_id: str) -> list[tuple[str, str]]:
        return [
            binding
            for binding in self._required.get(operation_id, set())
            if (operation_id, *binding) not in self._values
        ]

    def overrides_for(self, operation_id: str) -> dict[tuple[str, str], Any]:
        return {
            binding: self._values[(operation_id, *binding)].value
            for binding in self._required.get(operation_id, set())
            if (operation_id, *binding) in self._values
        }

    def capture(self, case: CompiledTestCase, result: TestResult) -> None:
        for variable in extract_link_variables(case, result, self._catalog):
            key = (
                variable.consumer_operation_id,
                variable.parameter_location,
                variable.parameter_name,
            )
            self._values[key] = variable


def extract_link_variables(
    case: CompiledTestCase,
    result: TestResult,
    catalog: OperationCatalog,
) -> list[WorkingVariable]:
    if result.actual_status is None:
        return []
    producer = catalog.get(case.operation_id)
    response = _response(producer, result.actual_status)
    if response is None:
        return []
    variables: list[WorkingVariable] = []
    for link in response.links.values():
        consumer = catalog.operations.get(link.operation_id)
        if consumer is None:
            raise VariableError(f"Link targets unknown operation {link.operation_id!r}")
        for name, expression in link.parameters.items():
            matches = [item for item in consumer.parameters if item.name == name]
            if len(matches) != 1:
                raise VariableError(
                    f"Link parameter {name!r} does not identify exactly one consumer parameter"
                )
            variables.append(
                WorkingVariable(
                    consumer_operation_id=consumer.operation_id,
                    parameter_location=matches[0].location,
                    parameter_name=name,
                    value=_extract(result.response_body, expression),
                    producer_operation_id=producer.operation_id,
                    producer_case_id=case.case_id,
                    response_expression=expression,
                    link_source=link.source_pointer,
                )
            )
    return variables
