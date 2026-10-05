"""Build compact catalog context and ask an LLM for workflow proposals."""

from __future__ import annotations

from typing import Any

from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.openapi.models import OperationCatalog


def build_catalog_context(catalog: OperationCatalog) -> dict[str, object]:
    """Expose semantic API structure without OpenAPI Links or raw document noise."""
    operations: list[dict[str, Any]] = []
    for operation in catalog.operations.values():
        operations.append(
            {
                "operation_id": operation.operation_id,
                "method": operation.method,
                "path": operation.path,
                "summary": operation.summary,
                "description": operation.description,
                "parameters": [
                    {
                        "name": parameter.name,
                        "location": parameter.location,
                        "required": parameter.required,
                        "description": parameter.description,
                        "schema": parameter.json_schema,
                    }
                    for parameter in operation.parameters
                ],
                "request_body": (
                    {
                        "required": operation.request_body.required,
                        "media_type": operation.request_body.media_type,
                        "schema": operation.request_body.json_schema,
                    }
                    if operation.request_body
                    else None
                ),
                "responses": [
                    {
                        "status_code": response.status_code,
                        "description": response.description,
                        "media_type": response.media_type,
                        "schema": response.json_schema,
                    }
                    for response in operation.responses
                ],
            }
        )
    return {
        "api": {"title": catalog.title, "version": catalog.version},
        "operations": operations,
    }


def build_workflow_prompt(catalog: OperationCatalog) -> dict[str, object]:
    return {
        "task": "Propose useful two-to-four-step API workflows by inferring producer-consumer relationships from operation semantics and schemas. Output proposals only; do not generate code or URLs.",
        "constraints": [
            "Use only operation_id values present in the catalog.",
            "Extract variables only from declared response body fields.",
            "Bind variables only to declared operation parameters.",
            "A variable must be extracted by an earlier step before it is bound.",
            "Prefer lifecycle workflows such as create-then-read over unrelated operations.",
            "Report uncertainty in ambiguities instead of inventing missing API behavior.",
        ],
        "runtime_expression": "Use $response.body#/field JSON Pointer expressions.",
        "catalog": build_catalog_context(catalog),
        "output_json_schema": WorkflowDiscovery.model_json_schema(),
    }


def discover_workflows(
    catalog: OperationCatalog,
    gateway: ModelGateway,
) -> ModelResponse[WorkflowDiscovery]:
    """Return typed proposals only; semantic validation and execution belong to later stages."""
    return gateway.complete(
        build_workflow_prompt(catalog),
        response_model=WorkflowDiscovery,
        trace_label="workflow-discovery",
    )
