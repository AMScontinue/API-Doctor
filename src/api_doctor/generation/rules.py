"""Load optional user-authored business rules for semantic test discovery."""

from __future__ import annotations

from pathlib import Path

import yaml

from api_doctor.testspec.models import RuleRecord


def load_business_rules(path: Path | None) -> list[RuleRecord]:
    if path is None:
        return []
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        raise ValueError("business rule file must contain a top-level rules list")
    return [RuleRecord.model_validate(item) for item in document["rules"]]
