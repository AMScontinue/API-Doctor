"""Small operation catalog loader for M0; full OpenAPI parsing belongs to M1."""

from __future__ import annotations

import json
from pathlib import Path

from api_doctor.testspec.models import Operation


def load_operations(path: Path) -> dict[str, Operation]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    operations = [Operation.model_validate(item) for item in raw["operations"]]
    return {operation.operation_id: operation for operation in operations}
