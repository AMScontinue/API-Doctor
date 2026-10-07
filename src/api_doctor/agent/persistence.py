"""Persist Agent checkpoints, trajectory events, and deterministic reports."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from api_doctor.agent.models import AnalysisRecord, AgentActionRecord, AgentRun, AgentState
from api_doctor.agent.pipeline import AgentPipelineResult
from api_doctor.observability import redact


class AgentArtifactStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        if output_dir.exists() and not output_dir.is_dir():
            raise FileExistsError(f"output path is not a directory: {output_dir}")
        if output_dir.exists() and any(output_dir.iterdir()):
            raise FileExistsError(f"output directory is not empty: {output_dir}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = output_dir / "checkpoint.json"
        self.trajectory_path = output_dir / "trajectory.jsonl"

    def checkpoint(self, state: AgentState, record: AgentActionRecord) -> None:
        payload = redact(
            {
                "state": state.model_dump(mode="json"),
                "last_record": record.model_dump(mode="json"),
            }
        )
        temporary = self.checkpoint_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint_path)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "action",
            "run_id": state.run_id,
            "plan_version": state.plan_version,
            "phase": state.phase,
            "action": record.action.model_dump(mode="json"),
            "observation": record.observation.model_dump(mode="json"),
            "execution": record.model_dump(mode="json"),
            "request_fingerprints": record.request_fingerprints,
            "remaining_test_budget": state.remaining_test_budget,
            "remaining_step_budget": state.remaining_step_budget,
        }
        with self.trajectory_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(redact(event), ensure_ascii=False) + "\n")

    def checkpoint_analysis(self, state: AgentState, record: AnalysisRecord) -> None:
        payload = redact(
            {
                "state": state.model_dump(mode="json"),
                "last_analysis": record.model_dump(mode="json"),
            }
        )
        temporary = self.checkpoint_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint_path)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "analysis",
            "run_id": state.run_id,
            "plan_version": state.plan_version,
            "phase": state.phase,
            "analysis": record.model_dump(mode="json"),
            "remaining_test_budget": state.remaining_test_budget,
            "remaining_step_budget": state.remaining_step_budget,
        }
        with self.trajectory_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(redact(event), ensure_ascii=False) + "\n")

    def finalize(self, run: AgentRun) -> dict[str, Path]:
        run_path = self.output_dir / "run.json"
        report_path = self.output_dir / "report.md"
        run_path.write_text(
            json.dumps(redact(run.model_dump(mode="json")), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        report_path.write_text(render_agent_report(run), encoding="utf-8")
        if not self.trajectory_path.exists():
            self.trajectory_path.touch()
        if not self.checkpoint_path.exists():
            self.checkpoint_path.write_text(
                json.dumps(
                    redact({"state": run.state.model_dump(mode="json"), "last_record": None}),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return {
            "run": run_path,
            "checkpoint": self.checkpoint_path,
            "trajectory": self.trajectory_path,
            "report": report_path,
        }

    def finalize_pipeline(self, result: AgentPipelineResult) -> dict[str, Path]:
        paths = self.finalize(result.run)
        pipeline_path = self.output_dir / "pipeline.json"
        pipeline_path.write_text(
            json.dumps(redact(_pipeline_payload(result)), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        paths["pipeline"] = pipeline_path
        return paths


def _pipeline_payload(result: AgentPipelineResult) -> dict[str, object]:
    return {
        "semantic_discovery": result.semantic_discovery.model_dump(mode="json"),
        "test_plan": result.test_plan.model_dump(mode="json"),
        "workflow_discovery": result.workflow_discovery.model_dump(mode="json"),
        "workflow_validation": result.workflow_validation.model_dump(mode="json"),
        "planning": result.planning.model_dump(mode="json"),
        "model_usage": {
            name: asdict(usage) for name, usage in result.model_usage.items()
        },
    }


def render_agent_report(run: AgentRun) -> str:
    state = run.state
    lines = [
        "# API Doctor Agent run",
        "",
        f"- Run ID: `{state.run_id}`",
        f"- Phase: `{state.phase}`",
        f"- Plan version: `{state.plan_version}`",
        f"- Strategy: {state.strategy}",
        f"- Remaining request budget: `{state.remaining_test_budget}`",
        f"- Remaining Agent step budget: `{state.remaining_step_budget}`",
        f"- Termination reason: `{state.termination_reason}`",
        "",
        "## Observations",
        "",
    ]
    if not state.observations:
        lines.append("No action was executed.")
    for observation in state.observations:
        lines.extend(
            [
                f"### `{observation.action_kind}:{observation.action_id}`",
                "",
                f"- Outcome: `{observation.outcome}`",
                f"- Requests: `{observation.request_count}`",
                f"- Agent steps: `{observation.agent_steps}`",
                f"- Evidence: `{observation.evidence_ref}`",
                f"- Summary: {observation.summary}",
            ]
        )
        lines.extend(f"- Violation: {item}" for item in observation.violations)
        lines.append("")
    lines.extend(["## Analyzer decisions", ""])
    if not run.analyses:
        lines.append("No failure required LLM analysis.")
    for index, analysis in enumerate(run.analyses, start=1):
        proposed = analysis.proposed
        lines.extend(
            [
                f"### Decision {index}",
                "",
                f"- Proposed: `{proposed.decision if proposed else 'unavailable'}`",
                f"- Applied: `{analysis.applied_decision}`",
            ]
        )
        if proposed is not None:
            lines.append(f"- Reason: {proposed.reason}")
        if analysis.analyzer_error is not None:
            lines.append(f"- Analyzer error: {analysis.analyzer_error}")
        if proposed is not None and proposed.hypothesis is not None:
            lines.append(f"- Hypothesis: {proposed.hypothesis.statement}")
        if analysis.added_action is not None:
            lines.append(
                f"- Added action: `{analysis.added_action.kind}:"
                f"{analysis.added_action.item_id}`"
            )
        lines.extend(f"- Rejected by validator: {item}" for item in analysis.validation_errors)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
