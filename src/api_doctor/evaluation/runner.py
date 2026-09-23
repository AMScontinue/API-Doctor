"""Orchestrate the M0 extraction, validation, compilation, and execution spike."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from api_doctor.execution.http import execute_case
from api_doctor.generation.semantic_extractor import Extractor
from api_doctor.openapi.catalog import load_operations
from api_doctor.testspec.compiler import CompileError, compile_candidate
from api_doctor.testspec.models import RuleRecord, SpikeMetrics


def load_rules(path: Path) -> list[RuleRecord]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [RuleRecord.model_validate(item) for item in raw["rules"]]


def run_spike(
    *,
    rules_path: Path,
    operations_path: Path,
    extractor: Extractor,
    base_url: str | None,
    seed: int,
    anchor: datetime,
    mode: str,
) -> tuple[SpikeMetrics, list[dict[str, object]]]:
    rules = load_rules(rules_path)
    operations = load_operations(operations_path)
    structured = compilable = compiled = executed = passed = failed = rejected = invalid_refs = 0
    records: list[dict[str, object]] = []
    for index, rule in enumerate(rules, start=1):
        record: dict[str, object] = {"rule_id": rule.id}
        try:
            candidate = extractor.extract(rule, operations)
            structured += 1
            record["candidate"] = candidate.model_dump(mode="json")
            if candidate.status == "not_compilable":
                rejected += 1
                record["outcome"] = "rejected"
                records.append(record)
                continue
            compilable += 1
            case = compile_candidate(
                candidate,
                operations,
                case_number=index,
                seed=seed,
                anchor=anchor,
            )
            compiled += 1
            record["compiled_case"] = case.model_dump(mode="json")
            if base_url:
                result = execute_case(case, base_url)
                executed += 1
                passed += result.outcome == "passed"
                failed += result.outcome == "failed"
                record["result"] = result.model_dump(mode="json")
            record["outcome"] = "compiled"
        except (ValidationError, CompileError, KeyError, ValueError) as error:
            if "unknown operation_id" in str(error) or "unknown field path" in str(error):
                invalid_refs += 1
            record["outcome"] = "error"
            record["error"] = str(error)
        records.append(record)
    metrics = SpikeMetrics(
        mode=mode,
        total_rules=len(rules),
        structured_successes=structured,
        compilable_candidates=compilable,
        compiled_cases=compiled,
        executed_cases=executed,
        passed_cases=passed,
        failed_cases=failed,
        rejected_rules=rejected,
        invalid_reference_count=invalid_refs,
    )
    return metrics, records


def save_results(path: Path, metrics: SpikeMetrics, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metrics": metrics.model_dump(), "ratios": metrics.ratios(), "records": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
