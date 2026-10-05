"""Adapt the synthetic M0 operation catalog to production OpenAPI models."""

from __future__ import annotations

import json
from pathlib import Path

from api_doctor.evaluation.models import BenchmarkOperation
from api_doctor.openapi.models import (
    Operation,
    OperationCatalog,
    RequestBody,
)


def load_operations(path: Path) -> dict[str, BenchmarkOperation]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    operations = [BenchmarkOperation.model_validate(item) for item in raw["operations"]]
    return {operation.operation_id: operation for operation in operations}


def catalog_from_operations(operations: dict[str, BenchmarkOperation]) -> OperationCatalog:
    """Convert benchmark input so it uses the production TestSpec compiler."""
    normalized = {
        operation_id: Operation(
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
            request_body=RequestBody(
                required=True,
                media_type="application/json",
                schema=operation.request_schema,
            ),
            source_pointer=f"#/benchmark/{operation.operation_id}",
        )
        for operation_id, operation in operations.items()
    }
    return OperationCatalog(
        title="M0 benchmark",
        version="1",
        openapi_version="3.1.0",
        source="benchmark",
        fingerprint="benchmark",
        operations=normalized,
    )
