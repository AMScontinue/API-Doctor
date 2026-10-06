"""Persist Agent checkpoints, trajectory events, and deterministic reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from api_doctor.agent.models import AgentActionRecord, AgentRun, AgentState
from api_doctor.observability import redact


class AgentArtifactStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = output_dir / "checkpoint.json"
        self.trajectory_path = output_dir / "trajectory.jsonl"
        self.trajectory_path.write_text("", encoding="utf-8")

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

    def finalize(self, run: AgentRun) -> dict[str, Path]:
        run_path = self.output_dir / "run.json"
        report_path = self.output_dir / "report.md"
        run_path.write_text(
            json.dumps(redact(run.model_dump(mode="json")), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        report_path.write_text(render_agent_report(run), encoding="utf-8")
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
    return "\n".join(lines).rstrip() + "\n"
