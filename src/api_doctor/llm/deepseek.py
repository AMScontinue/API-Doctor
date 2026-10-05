"""DeepSeek implementation of validated structured model calls."""

from __future__ import annotations

import json
import logging
import os
from time import perf_counter
from typing import Any
from urllib.request import Request, urlopen

from pydantic import TypeAdapter, ValidationError

from api_doctor.generation.semantic_extractor import ExtractionResult
from api_doctor.generation.semantic_prompt import build_semantic_prompt
from api_doctor.llm.gateway import (
    ModelCallTelemetry,
    ModelResponse,
    StructuredOutput,
)
from api_doctor.observability import json_for_log
from api_doctor.testspec.models import RuleRecord, SemanticTestCandidate
from api_doctor.evaluation.models import BenchmarkOperation


logger = logging.getLogger(__name__)


class DeepSeekGateway:
    """Call DeepSeek and validate one typed JSON response, with one repair attempt."""

    def __init__(self) -> None:
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not self._api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required")
        self._model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self._url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")

    def complete(
        self,
        prompt: dict[str, object],
        *,
        response_model: type[StructuredOutput],
        trace_label: str,
    ) -> ModelResponse[StructuredOutput]:
        return self._complete(
            prompt,
            response_model=response_model,
            trace_label=trace_label,
            repair=False,
        )

    def _complete(
        self,
        prompt: dict[str, object],
        *,
        response_model: type[StructuredOutput],
        trace_label: str,
        repair: bool,
    ) -> ModelResponse[StructuredOutput]:
        attempt = "structure_repair" if repair else "initial"
        logger.info(
            "[LLM INPUT] task=%s attempt=%s\n%s",
            trace_label,
            attempt,
            json_for_log(prompt),
        )
        raw, duration_ms = self._request(prompt)
        content = raw["choices"][0]["message"]["content"]
        telemetry = self._telemetry(raw, duration_ms)
        try:
            value = TypeAdapter(response_model).validate_json(content)
        except ValidationError as error:
            self._log_invalid_output(trace_label, attempt, duration_ms, content)
            if repair:
                raise
            repair_prompt = {
                **prompt,
                "task": "Repair the previous output to satisfy output_json_schema. Preserve the original intent and output JSON only.",
                "previous_invalid_output": content,
                "validation_error": str(error),
            }
            repaired = self._complete(
                repair_prompt,
                response_model=response_model,
                trace_label=trace_label,
                repair=True,
            )
            repair_telemetry = ModelCallTelemetry(
                calls=repaired.telemetry.calls,
                duration_ms=repaired.telemetry.duration_ms,
                prompt_tokens=repaired.telemetry.prompt_tokens,
                completion_tokens=repaired.telemetry.completion_tokens,
                structure_repair_attempts=1,
            )
            return ModelResponse(
                value=repaired.value,
                telemetry=telemetry + repair_telemetry,
            )

        logger.info(
            "[LLM OUTPUT] task=%s attempt=%s duration_ms=%d\n%s",
            trace_label,
            attempt,
            duration_ms,
            json_for_log(value.model_dump(mode="json")),
        )
        return ModelResponse(value=value, telemetry=telemetry)

    def _request(self, prompt: dict[str, object]) -> tuple[dict[str, Any], int]:
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You produce strictly typed JSON for a safe API testing agent. Output JSON only.",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        request = Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = perf_counter()
        with urlopen(request, timeout=60) as response:
            raw = json.loads(response.read().decode("utf-8"))
        return raw, round((perf_counter() - started) * 1000)

    @staticmethod
    def _telemetry(raw: dict[str, Any], duration_ms: int) -> ModelCallTelemetry:
        usage = raw.get("usage", {})
        return ModelCallTelemetry(
            calls=1,
            duration_ms=duration_ms,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    @staticmethod
    def _log_invalid_output(
        trace_label: str, attempt: str, duration_ms: int, content: str
    ) -> None:
        try:
            invalid_output: object = json.loads(content)
        except json.JSONDecodeError:
            invalid_output = "<invalid JSON omitted>"
        logger.info(
            "[LLM OUTPUT] task=%s attempt=%s duration_ms=%d valid=false\n%s",
            trace_label,
            attempt,
            duration_ms,
            json_for_log(invalid_output),
        )


class DeepSeekExtractor:
    """M0 adapter retained so historical semantic benchmarks use the shared gateway."""

    def __init__(self) -> None:
        self._gateway = DeepSeekGateway()

    def extract(
        self, rule: RuleRecord, operations: dict[str, BenchmarkOperation]
    ) -> ExtractionResult:
        response = self._gateway.complete(
            build_semantic_prompt(rule, operations),
            response_model=SemanticTestCandidate,
            trace_label=f"semantic-rule:{rule.id}",
        )
        return ExtractionResult(candidate=response.value, telemetry=response.telemetry)
