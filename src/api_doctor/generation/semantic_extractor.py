"""Semantic candidate extractor contract and deterministic benchmark fixture."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from api_doctor.testspec.models import Operation, RuleRecord, SemanticTestCandidate


@dataclass(frozen=True)
class ModelCallTelemetry:
    calls: int
    duration_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    structure_repair_attempts: int = 0


@dataclass(frozen=True)
class ExtractionResult:
    candidate: SemanticTestCandidate
    telemetry: ModelCallTelemetry | None = None


class Extractor(Protocol):
    def extract(self, rule: RuleRecord, operations: dict[str, Operation]) -> ExtractionResult: ...


class FixtureExtractor:
    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self._items = {item["rule_id"]: item for item in raw}

    def extract(self, rule: RuleRecord, operations: dict[str, Operation]) -> ExtractionResult:
        del operations
        candidate = SemanticTestCandidate.model_validate(self._items[rule.id])
        return ExtractionResult(candidate=candidate)
