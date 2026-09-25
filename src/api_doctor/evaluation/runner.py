"""Orchestrate the M0 extraction, validation, compilation, and execution spike."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from api_doctor.evaluation.models import EvaluationMetrics, ModelUsage, SpikeMetrics
from api_doctor.execution.http import execute_case
from api_doctor.generation.semantic_extractor import Extractor
from api_doctor.openapi.catalog import load_operations
from api_doctor.testspec.compiler import CompileError, compile_candidate
from api_doctor.testspec.models import RuleRecord
from api_doctor.testspec.validation import validate_candidate_context


logger = logging.getLogger(__name__)


def _matches_expectation(candidate: object, expectation: object) -> bool:
    if isinstance(expectation, str):
        return getattr(candidate, "status") == expectation
    if not isinstance(expectation, dict) or getattr(candidate, "status") != expectation.get("status"):
        return False
    if expectation.get("status") != "compilable":
        return True
    actual_mutations = [item.model_dump(mode="json") for item in candidate.mutations]

    def contains_all(required_mutations: list[dict[str, object]]) -> bool:
        return all(
            any(
                all(actual.get(key) == value for key, value in required.items())
                for actual in actual_mutations
            )
            for required in required_mutations
        )

    alternatives = expectation.get("mutation_alternatives")
    if alternatives:
        return any(contains_all(required) for required in alternatives)
    return contains_all(expectation.get("required_mutations", []))


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
    expected_outcomes_path: Path | None = None,
) -> tuple[SpikeMetrics, list[dict[str, object]]]:
    rules = load_rules(rules_path)
    operations = load_operations(operations_path)
    expected_outcomes = (
        json.loads(expected_outcomes_path.read_text(encoding="utf-8"))
        if expected_outcomes_path
        else {}
    )
    logger.debug(
        "[PIPELINE] loaded rules=%d operations=%d extractor_mode=%s",
        len(rules),
        len(operations),
        mode,
    )
    structured = compilable = compiled = executed = passed = failed = rejected = invalid_refs = 0
    model_usage = ModelUsage()
    evaluation = EvaluationMetrics()
    records: list[dict[str, object]] = []
    for index, rule in enumerate(rules, start=1):
        logger.debug(
            "[RULE %d/%d] start id=%s operation_hint=%s",
            index,
            len(rules),
            rule.id,
            rule.operation_id,
        )
        record: dict[str, object] = {"rule_id": rule.id}
        try:
            extraction = extractor.extract(rule, operations)
            candidate = extraction.candidate
            structured += 1
            telemetry = extraction.telemetry
            if telemetry:
                model_usage.calls += telemetry.calls
                model_usage.duration_ms += telemetry.duration_ms
                model_usage.prompt_tokens += telemetry.prompt_tokens
                model_usage.completion_tokens += telemetry.completion_tokens
                model_usage.structure_repair_attempts += telemetry.structure_repair_attempts
                record["model_usage"] = {
                    "calls": telemetry.calls,
                    "duration_ms": telemetry.duration_ms,
                    "prompt_tokens": telemetry.prompt_tokens,
                    "completion_tokens": telemetry.completion_tokens,
                    "structure_repair_attempts": telemetry.structure_repair_attempts,
                }
            expectation = expected_outcomes.get(rule.id)
            expected_outcome = (
                expectation.get("status") if isinstance(expectation, dict) else expectation
            )
            if expected_outcome == "compilable":
                if candidate.status == "compilable":
                    evaluation.correct_accepts += 1
                else:
                    evaluation.false_rejections += 1
            elif expected_outcome == "not_compilable":
                if candidate.status == "not_compilable":
                    evaluation.correct_rejections += 1
                else:
                    evaluation.false_accepts += 1
            record["candidate"] = candidate.model_dump(mode="json")
            validate_candidate_context(candidate, rule, operations)
            if candidate.status == "not_compilable":
                if expectation is not None:
                    if _matches_expectation(candidate, expectation):
                        evaluation.semantic_matches += 1
                    else:
                        evaluation.semantic_mismatches += 1
                rejected += 1
                record["outcome"] = "rejected"
                logger.debug(
                    "[RULE %d/%d] rejected id=%s reason=%s",
                    index,
                    len(rules),
                    rule.id,
                    candidate.unsupported_reason,
                )
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
            if expectation is not None:
                if _matches_expectation(candidate, expectation):
                    evaluation.semantic_matches += 1
                else:
                    evaluation.semantic_mismatches += 1
            record["compiled_case"] = case.model_dump(mode="json")
            if base_url:
                result = execute_case(case, base_url)
                executed += 1
                passed += result.outcome == "passed"
                failed += result.outcome == "failed"
                record["result"] = result.model_dump(mode="json")
            record["outcome"] = "compiled"
        except (ValidationError, CompileError, KeyError, TypeError, ValueError) as error:
            if expected_outcomes.get(rule.id) is not None:
                evaluation.semantic_mismatches += 1
            if "unknown operation_id" in str(error) or "unknown field path" in str(error):
                invalid_refs += 1
            record["outcome"] = "error"
            record["error"] = str(error)
            logger.debug(
                "[RULE %d/%d] error id=%s type=%s message=%s",
                index,
                len(rules),
                rule.id,
                type(error).__name__,
                error,
            )
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
        model_usage=model_usage,
        evaluation=evaluation,
    )
    return metrics, records


def save_results(path: Path, metrics: SpikeMetrics, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metrics": metrics.model_dump(), "ratios": metrics.ratios(), "records": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("[OUTPUT] saved result=%s", path)
