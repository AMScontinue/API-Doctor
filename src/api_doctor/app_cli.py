"""Main API Doctor command line interface."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.execution.runner import run_baseline
from api_doctor.openapi import load_openapi
from api_doctor.reporting.report import save_run_artifacts
from api_doctor.testspec.openapi_compiler import compile_openapi_candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="api-doctor")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="parse and normalize an OpenAPI document")
    inspect.add_argument("--spec", type=Path, required=True)
    inspect.add_argument("--output", type=Path, help="write normalized JSON to this file")
    candidates = commands.add_parser(
        "candidates", help="derive deterministic candidates from an OpenAPI document"
    )
    candidates.add_argument("--spec", type=Path, required=True)
    candidates.add_argument("--output", type=Path, help="write candidate JSON to this file")
    compile_command = commands.add_parser(
        "compile", help="compile deterministic OpenAPI candidates into concrete requests"
    )
    compile_command.add_argument("--spec", type=Path, required=True)
    compile_command.add_argument("--output", type=Path, help="write compiled cases to this file")
    compile_command.add_argument("--seed", type=int, default=20260924)
    compile_command.add_argument("--time-anchor", default="2026-10-02T10:00:00+00:00")
    run = commands.add_parser("run", help="run the deterministic OpenAPI baseline")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--max-tests", type=int, default=50)
    run.add_argument("--seed", type=int, default=20260924)
    run.add_argument("--time-anchor", default="2026-10-02T10:00:00+00:00")
    run.add_argument("--timeout", type=float, default=5.0)
    run.add_argument("--approve-delete", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    catalog = load_openapi(args.spec)
    if args.command == "run":
        baseline = run_baseline(
            catalog,
            base_url=args.base_url,
            seed=args.seed,
            anchor=datetime.fromisoformat(args.time_anchor),
            max_tests=args.max_tests,
            approve_delete=args.approve_delete,
            timeout_seconds=args.timeout,
        )
        artifacts = save_run_artifacts(baseline, args.output)
        print(
            json.dumps(
                {
                    "run_id": baseline.run_id,
                    "metrics": baseline.metrics.model_dump(mode="json"),
                    "artifacts": {key: str(path) for key, path in artifacts.items()},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    candidates = generate_schema_candidates(catalog) if args.command != "inspect" else []
    if args.command == "inspect":
        value = catalog.model_dump(mode="json")
    elif args.command == "candidates":
        value = [item.model_dump(mode="json") for item in candidates]
    else:
        anchor = datetime.fromisoformat(args.time_anchor)
        value = [
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
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
