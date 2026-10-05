"""User-facing API Doctor command line interface."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from api_doctor.config import load_dotenv
from api_doctor.execution.models import RunConfig
from api_doctor.execution.runner import run_baseline
from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.generation.rules import load_business_rules
from api_doctor.generation.semantic_discovery import discover_semantic_tests
from api_doctor.generation.workflow_discovery import discover_workflows
from api_doctor.llm.deepseek import DeepSeekGateway
from api_doctor.llm.gateway import ModelGateway, ModelResponse
from api_doctor.openapi import load_openapi
from api_doctor.openapi.models import OperationCatalog
from api_doctor.reporting.report import save_run_artifacts
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate


DEFAULT_SEED = 20260924
DEFAULT_TIME_ANCHOR = "2026-10-02T10:00:00+00:00"


def _add_spec(command: argparse.ArgumentParser, *, output_required: bool = False) -> None:
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument(
        "--output",
        type=Path,
        required=output_required,
        help="write the command artifact to this path",
    )


def _add_generation_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)
    command.add_argument("--time-anchor", default=DEFAULT_TIME_ANCHOR)


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
    discover.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

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

    run = commands.add_parser("run", help="execute the complete deterministic test pipeline")
    _add_spec(run, output_required=True)
    _add_generation_options(run)
    run.add_argument("--base-url", required=True)
    run.add_argument("--max-tests", type=int, default=50)
    run.add_argument("--timeout", type=float, default=5.0)
    run.add_argument("--approve-delete", action="store_true")
    return parser


def _write_json(value: Any, output: Path | None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if output is None:
        print(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")


def _run(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    """Execute the only end-to-end mode and persist its report artifacts."""
    config = RunConfig(
        base_url=args.base_url,
        seed=args.seed,
        time_anchor=datetime.fromisoformat(args.time_anchor),
        max_tests=args.max_tests,
        approve_delete=args.approve_delete,
        timeout_seconds=args.timeout,
    )
    result = run_baseline(catalog, config)
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
    response = _call_llm_discovery(args, lambda gateway: discover_workflows(catalog, gateway))
    _write_json(_model_response_payload("discovery", response), args.output)


def _discover_semantic_tests(args: argparse.Namespace, catalog: OperationCatalog) -> None:
    rules = load_business_rules(args.rules)
    response = _call_llm_discovery(
        args,
        lambda gateway: discover_semantic_tests(catalog, rules, gateway),
    )
    _write_json(_model_response_payload("discovery", response), args.output)


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
        "model_usage": {
            "calls": response.telemetry.calls,
            "duration_ms": response.telemetry.duration_ms,
            "prompt_tokens": response.telemetry.prompt_tokens,
            "completion_tokens": response.telemetry.completion_tokens,
            "structure_repair_attempts": response.telemetry.structure_repair_attempts,
        },
    }


def main() -> None:
    args = build_parser().parse_args()
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
    elif args.command == "discover-tests":
        _discover_semantic_tests(args, catalog)
    else:
        _run(args, catalog)


if __name__ == "__main__":
    main()
