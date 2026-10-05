"""Provider-independent contracts for validated structured model calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel


StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


@dataclass(frozen=True)
class ModelCallTelemetry:
    calls: int
    duration_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    structure_repair_attempts: int = 0

    def __add__(self, other: "ModelCallTelemetry") -> "ModelCallTelemetry":
        return ModelCallTelemetry(
            calls=self.calls + other.calls,
            duration_ms=self.duration_ms + other.duration_ms,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            structure_repair_attempts=(
                self.structure_repair_attempts + other.structure_repair_attempts
            ),
        )


@dataclass(frozen=True)
class ModelResponse(Generic[StructuredOutput]):
    value: StructuredOutput
    telemetry: ModelCallTelemetry


class ModelGateway(Protocol):
    def complete(
        self,
        prompt: dict[str, object],
        *,
        response_model: type[StructuredOutput],
        trace_label: str,
    ) -> ModelResponse[StructuredOutput]: ...
