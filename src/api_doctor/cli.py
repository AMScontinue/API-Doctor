"""CLI for the M0 technical spike."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from api_doctor.config import load_dotenv
from api_doctor.evaluation.runner import run_spike, save_results
from api_doctor.generation.semantic_extractor import FixtureExtractor
from api_doctor.llm.deepseek import DeepSeekExtractor


ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the M0 TestSpec technical spike")
    parser.add_argument("--mode", choices=("fixture", "deepseek"), default="fixture")
    parser.add_argument("--base-url")
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        default=ROOT / "benchmarks/m0",
        help="Directory containing rules.yaml, operations.json, and evaluation expectations",
    )
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--time-anchor", default="2026-10-02T10:00:00+00:00")
    parser.add_argument("--output", type=Path, default=ROOT / "runs/m0/result.json")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Controls the concise DeepSeek input/output log",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    load_dotenv(ROOT / ".env")
    fixture_dir = args.benchmark_dir
    extractor = (
        FixtureExtractor(fixture_dir / "compiler_fixtures.json")
        if args.mode == "fixture"
        else DeepSeekExtractor()
    )
    metrics, records = run_spike(
        rules_path=fixture_dir / "rules.yaml",
        operations_path=fixture_dir / "operations.json",
        extractor=extractor,
        base_url=args.base_url,
        seed=args.seed,
        anchor=datetime.fromisoformat(args.time_anchor),
        mode=args.mode,
        expected_outcomes_path=fixture_dir / "expected_outcomes.json",
    )
    save_results(args.output, metrics, records)
    logging.getLogger(__name__).info(
        "[RESULT]\n%s\nsaved: %s",
        json.dumps({"metrics": metrics.model_dump(), "ratios": metrics.ratios()}, indent=2),
        args.output,
    )


if __name__ == "__main__":
    main()
