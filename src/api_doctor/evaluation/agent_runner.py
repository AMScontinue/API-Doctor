"""Run isolated buggy-shop scenarios and score complete Agent behavior."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from api_doctor.agent.persistence import AgentArtifactStore
from api_doctor.agent.pipeline import AgentPipeline
from api_doctor.evaluation.agent_models import (
    AgentBenchmarkSuite,
    BenchmarkCheck,
    BenchmarkSummary,
    ScenarioResult,
)
from api_doctor.evaluation.agent_scoring import score_scenario
from api_doctor.execution.models import RunConfig
from api_doctor.generation.rules import load_business_rules
from api_doctor.llm.gateway import ModelGateway
from api_doctor.openapi import load_openapi


GatewayFactory = Callable[[], ModelGateway]


def load_agent_suite(path: Path) -> AgentBenchmarkSuite:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Agent benchmark suite must contain a YAML object")
    return AgentBenchmarkSuite.model_validate(payload)


def run_agent_benchmark(
    suite_path: Path,
    output_dir: Path,
    *,
    repetitions: int,
    gateway_factory: GatewayFactory,
    scenario_ids: set[str] | None = None,
    seed: int = 20260924,
    time_anchor: datetime = datetime.fromisoformat("2026-10-02T10:00:00+00:00"),
) -> BenchmarkSummary:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"benchmark output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    suite = load_agent_suite(suite_path)
    scenarios = [
        item for item in suite.scenarios if scenario_ids is None or item.id in scenario_ids
    ]
    unknown = (scenario_ids or set()) - {item.id for item in scenarios}
    if unknown:
        raise ValueError(f"unknown benchmark scenario(s): {sorted(unknown)!r}")
    if not scenarios:
        raise ValueError("benchmark selected no scenarios")
    root = suite_path.parent
    catalog = load_openapi((root / suite.spec).resolve())
    rules = load_business_rules((root / suite.rules).resolve())
    service_dir = (root / suite.service_dir).resolve()
    results: list[ScenarioResult] = []

    with tempfile.TemporaryDirectory(prefix="api-doctor-benchmark-") as temporary:
        binary = Path(temporary) / "buggy-shop"
        if any(item.start_service for item in scenarios):
            _build_buggy_shop(service_dir, binary)
        for scenario in scenarios:
            for repetition in range(1, repetitions + 1):
                started_at = time.monotonic()
                run_dir = output_dir / scenario.id / f"run-{repetition}"
                store = AgentArtifactStore(run_dir)
                service = None
                service_log = None
                try:
                    base_url = _allocate_base_url(suite.base_url)
                    if scenario.start_service:
                        service_log = (run_dir / "service.log").open("w", encoding="utf-8")
                        service = _start_buggy_shop(
                            binary,
                            service_dir,
                            base_url,
                            scenario.defects,
                            service_log,
                        )
                    else:
                        _require_unavailable(base_url)
                    pipeline_result = AgentPipeline(gateway_factory()).run(
                        catalog,
                        rules,
                        RunConfig(
                            base_url=base_url,
                            seed=seed,
                            time_anchor=time_anchor,
                            max_tests=scenario.max_tests,
                        ),
                        max_steps=scenario.max_steps,
                        checkpoint=store.checkpoint,
                        analysis_checkpoint=store.checkpoint_analysis,
                    )
                    store.finalize_pipeline(pipeline_result)
                    result = score_scenario(
                        scenario,
                        repetition,
                        pipeline_result,
                        str(run_dir),
                    )
                except Exception as error:
                    message = _safe_error(error)
                    result = ScenarioResult(
                        scenario_id=scenario.id,
                        repetition=repetition,
                        passed=False,
                        artifact_directory=str(run_dir),
                        duration_seconds=time.monotonic() - started_at,
                        execution_error=message,
                        checks=[
                            BenchmarkCheck(
                                name="execution_completed",
                                passed=False,
                                details=message,
                            )
                        ],
                    )
                finally:
                    if service is not None:
                        _stop_service(service)
                    if service_log is not None:
                        service_log.close()
                result = result.model_copy(
                    update={"duration_seconds": time.monotonic() - started_at}
                )
                results.append(result)
                _write_json(run_dir / "benchmark-result.json", result.model_dump(mode="json"))

    summary = _summarize(suite_path, repetitions, results)
    _write_json(output_dir / "summary.json", summary.model_dump(mode="json"))
    (output_dir / "report.md").write_text(_render_summary(summary), encoding="utf-8")
    return summary


def _build_buggy_shop(service_dir: Path, binary: Path) -> None:
    subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=service_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _start_buggy_shop(
    binary: Path,
    service_dir: Path,
    base_url: str,
    defects: list[str],
    log_stream,
) -> subprocess.Popen[str]:
    _require_unavailable(base_url)
    environment = os.environ.copy()
    environment["BUGGY_SHOP_DEFECTS"] = ",".join(defects)
    parsed = urlparse(base_url)
    environment["BUGGY_SHOP_ADDR"] = f"{parsed.hostname}:{parsed.port}"
    process = subprocess.Popen(
        [str(binary)],
        cwd=service_dir,
        env=environment,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_available(base_url, process)
    except Exception:
        _stop_service(process)
        raise
    return process


def _wait_until_available(
    base_url: str,
    process: subprocess.Popen[str],
    timeout_seconds: float = 10,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"buggy-shop exited with code {process.returncode}")
        if _is_available(base_url):
            return
        time.sleep(0.05)
    raise TimeoutError("buggy-shop did not start before benchmark timeout")


def _stop_service(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _require_unavailable(base_url: str) -> None:
    if _is_available(base_url):
        raise RuntimeError(f"benchmark base URL is already in use: {base_url}")


def _allocate_base_url(configured_url: str) -> str:
    """Use an isolated local port so interrupted benchmark runs cannot poison later runs."""
    parsed = urlparse(configured_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("benchmark service must use a local HTTP base URL")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((parsed.hostname, 0))
        port = listener.getsockname()[1]
    return f"http://{parsed.hostname}:{port}"


def _is_available(base_url: str) -> bool:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"invalid benchmark base URL: {base_url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=0.2):
            return True
    except OSError:
        return False


def _summarize(
    suite_path: Path,
    repetitions: int,
    results: list[ScenarioResult],
) -> BenchmarkSummary:
    scenario_ids = list(dict.fromkeys(item.scenario_id for item in results))
    rates = {
        scenario_id: (
            sum(item.passed for item in results if item.scenario_id == scenario_id)
            / sum(1 for item in results if item.scenario_id == scenario_id)
        )
        for scenario_id in scenario_ids
    }
    metrics = [item.metrics for item in results if item.metrics is not None]
    return BenchmarkSummary(
        suite=str(suite_path),
        repetitions=repetitions,
        total_runs=len(results),
        passed_runs=sum(item.passed for item in results),
        failed_runs=sum(not item.passed for item in results),
        duration_seconds=sum(item.duration_seconds for item in results),
        scenario_pass_rates=rates,
        total_llm_calls=sum(item.llm_calls for item in metrics),
        total_prompt_tokens=sum(item.prompt_tokens for item in metrics),
        total_completion_tokens=sum(item.completion_tokens for item in metrics),
        results=results,
    )


def _render_summary(summary: BenchmarkSummary) -> str:
    lines = [
        "# API Doctor Agent benchmark",
        "",
        f"- Runs: `{summary.total_runs}`",
        f"- Passed: `{summary.passed_runs}`",
        f"- Failed: `{summary.failed_runs}`",
        f"- Duration: `{summary.duration_seconds:.1f}s`",
        f"- LLM calls: `{summary.total_llm_calls}`",
        f"- Prompt tokens: `{summary.total_prompt_tokens}`",
        f"- Completion tokens: `{summary.total_completion_tokens}`",
        "",
        "## Scenario results",
        "",
    ]
    for result in summary.results:
        lines.extend(
            [
                f"### `{result.scenario_id}` run {result.repetition}",
                "",
                f"- Passed: `{result.passed}`",
                f"- Run ID: `{result.run_id}`",
                f"- Duration: `{result.duration_seconds:.1f}s`",
            ]
        )
        if result.execution_error:
            lines.append(f"- Execution error: {result.execution_error}")
        for check in result.checks:
            marker = "PASS" if check.passed else "FAIL"
            lines.append(f"- [{marker}] `{check.name}` — {check.details}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        locations = [
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_url=False, include_input=False)
        ]
        return f"ValidationError at {locations!r}"
    return type(error).__name__
