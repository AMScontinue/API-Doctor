"""Semantic candidate extractor contract and deterministic benchmark fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from api_doctor.testspec.models import Operation, RuleRecord, SemanticTestCandidate


class Extractor(Protocol):
    def extract(self, rule: RuleRecord, operations: dict[str, Operation]) -> SemanticTestCandidate: ...


class FixtureExtractor:
    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self._items = {item["rule_id"]: item for item in raw}

    def extract(self, rule: RuleRecord, operations: dict[str, Operation]) -> SemanticTestCandidate:
        del operations
        return SemanticTestCandidate.model_validate(self._items[rule.id])
