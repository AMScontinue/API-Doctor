"""Persist deterministic JSON, Markdown, and trajectory artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from api_doctor.execution.models import (
    BaselineRun,
    CaseRecord,
    WorkflowRun,
    WorkflowStepRecord,
)
from api_doctor.observability import redact
from api_doctor.testspec.models import CompiledTestCase, TestResult


def _curl_case(case: CompiledTestCase, result: TestResult) -> str | None:
    if not result.request_url:
        return None
    parts = ["curl", "-X", case.request.method, repr(result.request_url)]
    if case.request.media_type:
        parts.extend(["-H", repr(f"Content-Type: {case.request.media_type}")])
    body = redact(case.request.body)
    if body is not None:
        parts.extend(["--data", repr(json.dumps(body, ensure_ascii=False))])
    return " ".join(parts)


def _curl(record: CaseRecord) -> str | None:
    if record.compiled_case is None or record.result is None:
        return None
    return _curl_case(record.compiled_case, record.result)


def render_markdown(run: BaselineRun) -> str:
    metrics = run.metrics
    lines = [
        "# API Doctor deterministic run",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Spec: `{run.spec_source}`",
        f"- Spec fingerprint: `{run.spec_fingerprint}`",
        f"- Base URL: `{run.base_url}`",
        f"- Seed: `{run.seed}`",
        f"- Time anchor: `{run.time_anchor.isoformat()}`",
        "",
        "## Summary",
        "",
        "| Operations | Candidates | Compiled | Executed | Passed | Failed | Errors | Blocked |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {metrics.operations} | {metrics.candidates} | {metrics.compiled} | {metrics.executed} | {metrics.passed} | {metrics.failed} | {metrics.errors} | {metrics.blocked} |",
        "",
    ]
    findings = [
        record
        for record in run.records
        if record.state != "executed"
        or record.result is None
        or record.result.outcome != "passed"
    ]
    lines.extend(["## Failures and blocked cases", ""])
    if not findings:
        lines.append("No failures or blocked cases.")
    for record in findings:
        result = record.result
        lines.extend(
            [
                f"### `{record.candidate.rule_id}`",
                "",
                f"- State: `{record.state}`",
                f"- Purpose: {record.candidate.purpose}",
                f"- Source: `{record.candidate.source.kind}`",
                f"- Evidence: `{record.candidate.source.id}`",
            ]
        )
        if record.reason:
            lines.append(f"- Reason: {record.reason}")
        if result:
            lines.append(f"- Result: `{result.outcome}`")
            lines.append(f"- Status: `{result.actual_status}`; expected `{result.expected_statuses}`")
            for violation in result.violations:
                lines.append(f"- Violation: {violation}")
        curl = _curl(record)
        if curl:
            lines.extend(["", "```bash", curl, "```"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_run_artifacts(run: BaselineRun, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = redact(run.model_dump(mode="json"))
    run_json = output_dir / "run.json"
    report_md = output_dir / "report.md"
    trajectory = output_dir / "trajectory.jsonl"
    run_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(run), encoding="utf-8")
    with trajectory.open("w", encoding="utf-8") as stream:
        for step, record in enumerate(run.records, start=1):
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run.run_id,
                "step": step,
                "type": "tool" if record.state == "executed" else "error",
                "name": "execute_test" if record.compiled_case else "compile_test",
                "input": redact(
                    record.compiled_case.request.model_dump(mode="json")
                    if record.compiled_case
                    else {"rule_id": record.candidate.rule_id}
                ),
                "output_summary": (
                    record.result.model_dump(mode="json") if record.result else record.reason
                ),
                "duration_ms": record.result.duration_ms if record.result else 0,
                "success": bool(record.result and record.result.outcome == "passed"),
            }
            stream.write(json.dumps(redact(event), ensure_ascii=False, default=str) + "\n")
    return {"json": run_json, "markdown": report_md, "trajectory": trajectory}


def _workflow_curl(record: WorkflowStepRecord) -> str | None:
    if record.compiled_case is None or record.result is None:
        return None
    return _curl_case(record.compiled_case, record.result)


def render_workflow_markdown(run: WorkflowRun) -> str:
    metrics = run.metrics
    lines = [
        "# API Doctor workflow run",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Workflow: `{run.workflow_id}`",
        f"- Spec fingerprint: `{run.spec_fingerprint}`",
        f"- Base URL: `{run.base_url}`",
        "",
        "## Summary",
        "",
        "| Steps | Executed | Passed | Failed | Errors | Blocked |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {metrics.total_steps} | {metrics.executed} | {metrics.passed} | {metrics.failed} | {metrics.errors} | {metrics.blocked} |",
        "",
        "## Steps",
        "",
    ]
    for record in run.records:
        result = record.result
        lines.extend(
            [
                f"### `{record.step.step_id}` — `{record.step.operation_id}`",
                "",
                f"- State: `{record.state}`",
                f"- Purpose: {record.step.purpose}",
                f"- Extracted variables: `{record.extracted_variables}`",
            ]
        )
        if result:
            lines.append(f"- Result: `{result.outcome}`")
            lines.append(
                f"- Status: `{result.actual_status}`; expected `{result.expected_statuses}`"
            )
        if record.reason:
            lines.append(f"- Reason: {record.reason}")
        curl = _workflow_curl(record)
        if curl:
            lines.extend(["", "```bash", curl, "```"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_workflow_run_artifacts(run: WorkflowRun, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_json = output_dir / "run.json"
    report_md = output_dir / "report.md"
    trajectory = output_dir / "trajectory.jsonl"
    run_json.write_text(
        json.dumps(redact(run.model_dump(mode="json")), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_md.write_text(render_workflow_markdown(run), encoding="utf-8")
    with trajectory.open("w", encoding="utf-8") as stream:
        for index, record in enumerate(run.records, start=1):
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run.run_id,
                "step": index,
                "type": "tool" if record.compiled_case else "blocked",
                "name": "execute_workflow_step",
                "operation_id": record.step.operation_id,
                "input": (
                    record.compiled_case.request.model_dump(mode="json")
                    if record.compiled_case
                    else None
                ),
                "output": record.result.model_dump(mode="json") if record.result else record.reason,
                "extracted_variables": record.extracted_variables,
            }
            stream.write(json.dumps(redact(event), ensure_ascii=False) + "\n")
    return {"json": run_json, "markdown": report_md, "trajectory": trajectory}
