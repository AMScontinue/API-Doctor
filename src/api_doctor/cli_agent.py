"""CLI adapter for Agent planning and execution commands."""

from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from api_doctor.agent.models import AgentRun
from api_doctor.agent.persistence import AgentArtifactStore
from api_doctor.agent.pipeline import AgentPipeline
from api_doctor.agent.planning import initialize_agent_state, propose_initial_plan
from api_doctor.agent.runtime import run_agent_actions
from api_doctor.cli_support import (
    add_generation_options,
    add_spec_argument,
    load_agent_state,
    load_compiled_workflows,
    load_final_test_plan,
    run_config,
    write_json,
)
from api_doctor.config import load_dotenv
from api_doctor.generation.rules import load_business_rules
from api_doctor.llm.deepseek import DeepSeekGateway
from api_doctor.openapi.models import OperationCatalog


AGENT_COMMANDS = frozenset({"plan-agent", "run-agent-actions", "agent-run"})


def add_agent_commands(commands: Any) -> None:
    plan = commands.add_parser(
        "plan-agent",
        help="ask DeepSeek to prioritize validated tests and workflows without HTTP",
    )
    add_spec_argument(plan, output_required=True)
    plan.add_argument("--test-plan", type=Path, required=True)
    plan.add_argument("--workflows", type=Path, required=True)
    _add_agent_limits(plan)
    _add_llm_log_level(plan)

    actions = commands.add_parser(
        "run-agent-actions",
        help="execute a validated AgentState queue without additional LLM calls",
    )
    add_spec_argument(actions, output_required=True)
    add_generation_options(actions)
    actions.add_argument("--state", type=Path, required=True)
    actions.add_argument("--test-plan", type=Path, required=True)
    actions.add_argument("--workflows", type=Path, required=True)
    _add_http_options(actions)
    actions.add_argument("--approve-delete", action="store_true")

    run = commands.add_parser(
        "agent-run",
        help="discover, plan, and execute one complete Agent run",
    )
    add_spec_argument(run, output_required=True)
    add_generation_options(run)
    run.add_argument("--rules", type=Path)
    _add_agent_limits(run)
    _add_http_options(run)
    _add_llm_log_level(run)


def run_agent_command(
    args: argparse.Namespace,
    catalog: OperationCatalog,
) -> None:
    handler = {
        "plan-agent": _plan_agent,
        "run-agent-actions": _run_actions,
        "agent-run": _run_pipeline,
    }[args.command]
    handler(args, catalog)


def _plan_agent(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    plan = load_final_test_plan(args.test_plan)
    workflows = load_compiled_workflows(args.workflows)
    gateway = _gateway(args.log_level)
    response = propose_initial_plan(
        plan,
        workflows,
        max_tests=args.max_tests,
        max_steps=args.max_steps,
        delete_approved=args.approve_delete,
        gateway=gateway,
    )
    result = initialize_agent_state(
        spec_fingerprint=catalog.fingerprint,
        plan=plan,
        workflows=workflows,
        proposed_plan=response.value,
        max_tests=args.max_tests,
        max_steps=args.max_steps,
        delete_approved=args.approve_delete,
    )
    write_json(
        {
            "planning": result.model_dump(mode="json"),
            "model_usage": asdict(response.telemetry),
        },
        args.output,
    )


def _run_actions(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    state = load_agent_state(args.state)
    store = AgentArtifactStore(args.output)
    run = run_agent_actions(
        state,
        load_final_test_plan(args.test_plan),
        load_compiled_workflows(args.workflows),
        catalog,
        run_config(args, max_tests=max(state.remaining_test_budget, 1)),
        checkpoint=store.checkpoint,
    )
    _print_run(run, store.finalize(run))


def _run_pipeline(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    store = AgentArtifactStore(args.output)
    result = AgentPipeline(_gateway(args.log_level)).run(
        catalog,
        load_business_rules(args.rules),
        run_config(args),
        max_steps=args.max_steps,
        checkpoint=store.checkpoint,
    )
    artifacts = store.finalize_pipeline(result)
    write_json(
        {
            "run_id": result.run.state.run_id,
            "phase": result.run.state.phase,
            "test_plan": result.test_plan.summary(),
            "compiled_workflows": len(result.workflows),
            "observations": len(result.run.state.observations),
            "termination_reason": result.run.state.termination_reason,
            "model_usage": {
                name: asdict(usage) for name, usage in result.model_usage.items()
            },
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
    )


def _print_run(run: AgentRun, artifacts: dict[str, Path]) -> None:
    write_json(
        {
            "run_id": run.state.run_id,
            "phase": run.state.phase,
            "observations": len(run.state.observations),
            "remaining_test_budget": run.state.remaining_test_budget,
            "remaining_step_budget": run.state.remaining_step_budget,
            "termination_reason": run.state.termination_reason,
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        }
    )


def _gateway(log_level: str) -> DeepSeekGateway:
    load_dotenv(Path(".env"))
    logging.basicConfig(level=getattr(logging, log_level), format="%(message)s")
    return DeepSeekGateway()


def _add_agent_limits(command: argparse.ArgumentParser) -> None:
    command.add_argument("--max-tests", type=int, default=10)
    command.add_argument("--max-steps", type=int, default=20)
    command.add_argument("--approve-delete", action="store_true")


def _add_http_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--base-url", required=True)
    command.add_argument("--timeout", type=float, default=5.0)


def _add_llm_log_level(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
