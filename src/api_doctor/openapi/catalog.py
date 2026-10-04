"""Compatibility loader for the isolated M0 benchmark catalog."""

from __future__ import annotations

import json
from pathlib import Path

from api_doctor.testspec.models import Operation


def load_operations(path: Path) -> dict[str, Operation]:
    """Load M0's synthetic operation list; real specifications use load_openapi."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    operations = [Operation.model_validate(item) for item in raw["operations"]]
    return {operation.operation_id: operation for operation in operations}
