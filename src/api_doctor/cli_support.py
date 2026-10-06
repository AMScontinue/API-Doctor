"""Small shared helpers for CLI adapters."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from api_doctor.agent.models import AgentState
from api_doctor.execution.models import RunConfig
from api_doctor.testspec.fusion import FinalTestPlan
from api_doctor.workflows.validation import CompiledWorkflow, WorkflowValidationResult


def add_spec_argument(
    command: argparse.ArgumentParser,
    *,
    output_required: bool = False,
) -> None:
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument(
        "--output",
        type=Path,
        required=output_required,
        help="artifact file or run directory, depending on the command",
    )


def add_generation_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--seed", type=int, default=20260924)
    command.add_argument("--time-anchor", default="2026-10-02T10:00:00+00:00")


def write_json(value: Any, output: Path | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if output is None:
        print(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")


def run_config(args: argparse.Namespace, *, max_tests: int | None = None) -> RunConfig:
    return RunConfig(
        base_url=args.base_url,
        seed=args.seed,
        time_anchor=datetime.fromisoformat(args.time_anchor),
        max_tests=max_tests if max_tests is not None else args.max_tests,
        approve_delete=args.approve_delete,
        timeout_seconds=args.timeout,
    )


def load_agent_state(path: Path) -> AgentState:
    payload = _read_object(path)
    planning = payload.get("planning")
    if isinstance(planning, dict):
        payload = planning
    state = payload.get("state")
    return AgentState.model_validate(state if isinstance(state, dict) else payload)


def load_final_test_plan(path: Path) -> FinalTestPlan:
    payload = _read_object(path)
    plan = payload.get("plan")
    return FinalTestPlan.model_validate(plan if isinstance(plan, dict) else payload)


def load_compiled_workflows(path: Path) -> list[CompiledWorkflow]:
    payload = _read_object(path)
    validation = WorkflowValidationResult.model_validate(
        {"reviews": payload.get("reviews", [])}
    )
    return validation.compiled_workflows()


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"artifact must contain a JSON object: {path}")
    return payload
