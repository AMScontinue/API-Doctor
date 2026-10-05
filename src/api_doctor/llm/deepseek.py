"""DeepSeek-backed extraction of semantic TestSpec candidates."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from urllib.request import Request, urlopen

from pydantic import TypeAdapter, ValidationError

from api_doctor.evaluation.models import BenchmarkOperation
from api_doctor.generation.semantic_extractor import ExtractionResult, ModelCallTelemetry
from api_doctor.generation.semantic_prompt import (
    build_prompt_log_view,
    build_repair_prompt,
    build_semantic_prompt,
)
from api_doctor.observability import json_for_log
from api_doctor.testspec.models import RuleRecord, SemanticTestCandidate


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StructuredCompletion:
    candidate: SemanticTestCandidate
    telemetry: ModelCallTelemetry


def _merge_telemetry(
    first: ModelCallTelemetry, second: ModelCallTelemetry
) -> ModelCallTelemetry:
    return ModelCallTelemetry(
        calls=first.calls + second.calls,
        duration_ms=first.duration_ms + second.duration_ms,
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
        structure_repair_attempts=(
            first.structure_repair_attempts + second.structure_repair_attempts
        ),
    )


class DeepSeekExtractor:
    def __init__(self) -> None:
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not self._api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for --mode deepseek")
        self._model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self._url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")

    def extract(
        self, rule: RuleRecord, operations: dict[str, BenchmarkOperation]
    ) -> ExtractionResult:
        completion = self._complete(
            build_semantic_prompt(rule, operations),
            rule_id=rule.id,
            attempt="initial",
        )
        return ExtractionResult(
            candidate=completion.candidate,
            telemetry=completion.telemetry,
        )

    def _complete(
        self, prompt: dict[str, object], *, rule_id: str, attempt: str
    ) -> _StructuredCompletion:
        logger.info(
            "[LLM INPUT] rule=%s attempt=%s\n%s",
            rule_id,
            attempt,
            json_for_log(build_prompt_log_view(prompt)),
        )
        raw, duration_ms = self._request(prompt)
        content = raw["choices"][0]["message"]["content"]
        telemetry = self._telemetry(raw, duration_ms)

        try:
            candidate = TypeAdapter(SemanticTestCandidate).validate_json(content)
        except ValidationError as error:
            self._log_invalid_output(rule_id, attempt, duration_ms, content)
            if attempt == "structure_repair":
                raise
            repaired = self._complete(
                build_repair_prompt(prompt, content, str(error)),
                rule_id=rule_id,
                attempt="structure_repair",
            )
            repair_telemetry = ModelCallTelemetry(
                calls=repaired.telemetry.calls,
                duration_ms=repaired.telemetry.duration_ms,
                prompt_tokens=repaired.telemetry.prompt_tokens,
                completion_tokens=repaired.telemetry.completion_tokens,
                structure_repair_attempts=(
                    repaired.telemetry.structure_repair_attempts + 1
                ),
            )
            return _StructuredCompletion(
                candidate=repaired.candidate,
                telemetry=_merge_telemetry(telemetry, repair_telemetry),
            )

        logger.info(
            "[LLM OUTPUT] rule=%s attempt=%s duration_ms=%d\n%s",
            rule_id,
            attempt,
            duration_ms,
            json_for_log(candidate.model_dump(mode="json")),
        )
        return _StructuredCompletion(candidate=candidate, telemetry=telemetry)

    def _request(self, prompt: dict[str, object]) -> tuple[dict[str, Any], int]:
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are a strict API testing TestSpec compiler. Output JSON only.",
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
        rule_id: str, attempt: str, duration_ms: int, content: str
    ) -> None:
        try:
            invalid_output: object = json.loads(content)
        except json.JSONDecodeError:
            invalid_output = "<invalid JSON omitted>"
        logger.info(
            "[LLM OUTPUT] rule=%s attempt=%s duration_ms=%d valid=false\n%s",
            rule_id,
            attempt,
            duration_ms,
            json_for_log(invalid_output),
        )
