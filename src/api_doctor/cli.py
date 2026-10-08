"""User-facing API Doctor command line interface."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from api_doctor.cli_agent import AGENT_COMMANDS, add_agent_commands, run_agent_command
from api_doctor.cli_support import (
    add_generation_options as _add_generation_options,
    add_spec_argument as _add_spec,
    run_config,
    write_json as _write_json,
)
from api_doctor.config import load_dotenv
from api_doctor.evaluation.agent_cli import (
    add_agent_benchmark_command,
    run_agent_benchmark_command,
)
from api_doctor.execution.runner import run_baseline
from api_doctor.execution.workflow_runner import run_workflow
from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.generation.rules import load_business_rules
from api_doctor.generation.semantic_discovery import SemanticDiscovery, discover_semantic_tests
from api_doctor.generation.workflow_discovery import discover_workflows
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.deepseek import DeepSeekGateway
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.openapi import load_openapi
from api_doctor.openapi.models import OperationCatalog
from api_doctor.reporting.report import save_run_artifacts, save_workflow_run_artifacts
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate
from api_doctor.testspec.plan_builder import build_final_test_plan
from api_doctor.testspec.semantic_validation import (
    SemanticValidationResult,
    validate_semantic_candidates,
)
from api_doctor.workflows.validation import validate_workflows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api-doctor",
        description="Test an OpenAPI service or inspect an intermediate pipeline artifact.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="show the normalized OpenAPI catalog")
    _add_spec(inspect)

    candidates = commands.add_parser("candidates", help="show generated test candidates")
    _add_spec(candidates)

    compile_command = commands.add_parser("compile", help="show compiled HTTP test cases")
    _add_spec(compile_command)
    _add_generation_options(compile_command)

    discover = commands.add_parser(
        "discover-workflows",
        help="ask DeepSeek for typed workflow proposals without executing them",
    )
    _add_spec(discover)
    discover.add_argument("--rules", type=Path)
    discover.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    validate_workflow = commands.add_parser(
        "validate-workflows",
        help="validate and compile a saved workflow discovery artifact without HTTP",
    )
    _add_spec(validate_workflow, output_required=True)
    validate_workflow.add_argument("--input", type=Path, required=True)

    run_workflow_command = commands.add_parser(
        "run-workflow",
        help="validate and execute one discovered workflow through restricted HTTP",
    )
    _add_spec(run_workflow_command, output_required=True)
    _add_generation_options(run_workflow_command)
    run_workflow_command.add_argument("--input", type=Path, required=True)
    run_workflow_command.add_argument("--workflow-id", required=True)
    run_workflow_command.add_argument("--base-url", required=True)
    run_workflow_command.add_argument("--max-tests", type=int, default=4)
    run_workflow_command.add_argument("--timeout", type=float, default=5.0)
    run_workflow_command.add_argument("--approve-delete", action="store_true")

    add_agent_commands(commands)
    add_agent_benchmark_command(commands)

    discover_tests = commands.add_parser(
        "discover-tests",
        help="ask DeepSeek for typed semantic test candidates without executing them",
    )
    _add_spec(discover_tests)
    discover_tests.add_argument("--rules", type=Path)
    discover_tests.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    validate_tests = commands.add_parser(
        "validate-tests",
        help="validate and compile a saved semantic discovery artifact without HTTP",
    )
    _add_spec(validate_tests)
    _add_generation_options(validate_tests)
    validate_tests.add_argument("--input", type=Path, required=True)
    validate_tests.add_argument("--rules", type=Path)

    fuse_tests = commands.add_parser(
        "fuse-tests",
        help="merge mechanical and validated semantic tests without HTTP",
    )
    _add_spec(fuse_tests, output_required=True)
    _add_generation_options(fuse_tests)
    fuse_tests.add_argument("--input", type=Path, required=True)
    fuse_tests.add_argument("--rules", type=Path)

    run = commands.add_parser("run", help="execute the complete deterministic test pipeline")
    _add_spec(run, output_required=True)
    _add_generation_options(run)
    run.add_argument("--base-url", required=True)
    run.add_argument("--max-tests", type=int, default=50)
    run.add_argument("--timeout", type=float, default=5.0)
    run.add_argument("--approve-delete", action="store_true")
    return parser


def _run(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    """Execute the deterministic baseline and persist its report artifacts."""
    result = run_baseline(catalog, run_config(args))
    artifacts = save_run_artifacts(result, args.output)
    _write_json(
        {
            "run_id": result.run_id,
            "metrics": result.metrics.model_dump(mode="json"),
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        },
        output=None,
    )


def _compile(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    candidates = generate_schema_candidates(catalog)
    anchor = datetime.fromisoformat(args.time_anchor)
    cases = [
        compile_openapi_candidate(
            candidate,
            catalog,
            case_number=index,
            seed=args.seed,
            anchor=anchor,
        ).model_dump(mode="json")
        for index, candidate in enumerate(candidates, start=1)
        if candidate.status == "compilable"
    ]
    _write_json(cases, args.output)


def _discover_workflows(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    response = _call_llm_discovery(
        args,
        lambda gateway: discover_workflows(
            catalog,
            gateway,
            load_business_rules(args.rules),
        ),
    )
    _write_json(_model_response_payload("discovery", response), args.output)


def _validate_workflows(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    discovery = _load_workflow_discovery(args.input)
    result = validate_workflows(discovery.workflows, catalog)
    _write_json(
        {
            "summary": result.summary(),
            "ambiguities": discovery.ambiguities,
            "reviews": [review.model_dump(mode="json") for review in result.reviews],
        },
        args.output,
    )


def _run_workflow(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    discovery = _load_workflow_discovery(args.input)
    validation = validate_workflows(discovery.workflows, catalog)
    review = next(
        (
            item
            for item in validation.reviews
            if item.workflow.workflow_id == args.workflow_id
        ),
        None,
    )
    if review is None:
        raise SystemExit(f"unknown workflow_id {args.workflow_id!r}")
    if review.compiled_workflow is None:
        raise SystemExit(
            f"workflow {args.workflow_id!r} failed validation: {'; '.join(review.errors)}"
        )
    run = run_workflow(
        review.compiled_workflow,
        catalog,
        run_config(args),
    )
    artifacts = save_workflow_run_artifacts(run, args.output)
    _write_json(
        {
            "run_id": run.run_id,
            "workflow_id": run.workflow_id,
            "metrics": run.metrics.model_dump(mode="json"),
            "artifacts": {name: str(path) for name, path in artifacts.items()},
        },
        output=None,
    )


def _load_workflow_discovery(path: Path) -> WorkflowDiscovery:
    payload = json.loads(path.read_text(encoding="utf-8"))
    discovery = payload.get("workflow_discovery", payload.get("discovery", payload))
    return WorkflowDiscovery.model_validate(discovery)


def _discover_semantic_tests(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    rules = load_business_rules(args.rules)
    response = _call_llm_discovery(
        args,
        lambda gateway: discover_semantic_tests(catalog, rules, gateway),
    )
    _write_json(_model_response_payload("discovery", response), args.output)


def _semantic_validation(
    args: argparse.Namespace,
    catalog: OperationCatalog,
) -> SemanticValidationResult:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    discovery = SemanticDiscovery.model_validate(payload.get("discovery", payload))
    return validate_semantic_candidates(
        discovery.candidates,
        catalog,
        load_business_rules(args.rules),
        seed=args.seed,
        anchor=datetime.fromisoformat(args.time_anchor),
    )


def _validate_semantic_tests(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    result = _semantic_validation(args, catalog)
    _write_json(
        {
            "summary": result.summary(),
            "reviews": [review.model_dump(mode="json") for review in result.reviews],
        },
        args.output,
    )


def _fuse_tests(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    discovery = SemanticDiscovery.model_validate(payload.get("discovery", payload))
    plan = build_final_test_plan(
        catalog,
        discovery.candidates,
        load_business_rules(args.rules),
        seed=args.seed,
        anchor=datetime.fromisoformat(args.time_anchor),
    )
    _write_json(
        {
            "summary": plan.summary(),
            "plan": plan.model_dump(mode="json"),
        },
        args.output,
    )


def _call_llm_discovery(
    args: argparse.Namespace,
    call: Callable[[ModelGateway], ModelResponse],
) -> ModelResponse:
    load_dotenv(Path(".env"))
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    return call(DeepSeekGateway())


def _model_response_payload(name: str, response: ModelResponse) -> dict[str, object]:
    return {
        name: response.value.model_dump(mode="json"),
        "model_usage": _model_usage(response),
    }


def _model_usage(response: ModelResponse) -> dict[str, int]:
    return {
        "calls": response.telemetry.calls,
        "duration_ms": response.telemetry.duration_ms,
        "prompt_tokens": response.telemetry.prompt_tokens,
        "completion_tokens": response.telemetry.completion_tokens,
        "structure_repair_attempts": response.telemetry.structure_repair_attempts,
    }


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "benchmark":
        run_agent_benchmark_command(args)
        return
    catalog = load_openapi(args.spec)

    # The three inspection commands expose stable pipeline boundaries for debugging.
    if args.command == "inspect":
        _write_json(catalog.model_dump(mode="json"), args.output)
    elif args.command == "candidates":
        candidates = generate_schema_candidates(catalog)
        _write_json([item.model_dump(mode="json") for item in candidates], args.output)
    elif args.command == "compile":
        _compile(args, catalog)
    elif args.command == "discover-workflows":
        _discover_workflows(args, catalog)
    elif args.command == "validate-workflows":
        _validate_workflows(args, catalog)
    elif args.command == "run-workflow":
        _run_workflow(args, catalog)
    elif args.command in AGENT_COMMANDS:
        run_agent_command(args, catalog)
    elif args.command == "discover-tests":
        _discover_semantic_tests(args, catalog)
    elif args.command == "validate-tests":
        _validate_semantic_tests(args, catalog)
    elif args.command == "fuse-tests":
        _fuse_tests(args, catalog)
    else:
        _run(args, catalog)


if __name__ == "__main__":
    main()
