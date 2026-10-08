"""CLI adapter for the complete Agent benchmark suite."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from api_doctor.config import load_dotenv
from api_doctor.evaluation.agent_runner import run_agent_benchmark
from api_doctor.llm.deepseek import DeepSeekGateway


def add_agent_benchmark_command(commands) -> None:
    command = commands.add_parser(
        "benchmark",
        help="run and deterministically score the end-to-end Agent benchmark suite",
    )
    command.add_argument("--suite", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--repetitions", type=int, default=1)
    command.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="run only this scenario id; repeat the option to select multiple",
    )
    command.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
    )


def run_agent_benchmark_command(args: argparse.Namespace) -> None:
    load_dotenv(Path(".env"))
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    summary = run_agent_benchmark(
        args.suite,
        args.output,
        repetitions=args.repetitions,
        gateway_factory=DeepSeekGateway,
        scenario_ids=set(args.scenarios) if args.scenarios else None,
    )
    print(summary.model_dump_json(indent=2))
