"""Main API Doctor command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from api_doctor.generation.schema_candidates import generate_schema_candidates
from api_doctor.openapi import load_openapi


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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    catalog = load_openapi(args.spec)
    value = (
        catalog.model_dump(mode="json")
        if args.command == "inspect"
        else [item.model_dump(mode="json") for item in generate_schema_candidates(catalog)]
    )
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
