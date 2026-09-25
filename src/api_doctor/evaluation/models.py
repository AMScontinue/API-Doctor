"""Metrics emitted by the M0 benchmark runner."""

from pydantic import BaseModel, Field


class ModelUsage(BaseModel):
    calls: int = 0
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    structure_repair_attempts: int = 0


class EvaluationMetrics(BaseModel):
    correct_accepts: int = 0
    correct_rejections: int = 0
    false_accepts: int = 0
    false_rejections: int = 0
    semantic_matches: int = 0
    semantic_mismatches: int = 0


class SpikeMetrics(BaseModel):
    mode: str
    total_rules: int
    structured_successes: int
    compilable_candidates: int
    compiled_cases: int
    executed_cases: int
    passed_cases: int
    failed_cases: int
    rejected_rules: int
    invalid_reference_count: int
    model_usage: ModelUsage = Field(default_factory=ModelUsage)
    evaluation: EvaluationMetrics = Field(default_factory=EvaluationMetrics)

    def ratios(self) -> dict[str, float]:
        denominator = self.total_rules or 1
        return {
            "structured_success_rate": self.structured_successes / denominator,
            "compile_rate": self.compiled_cases / denominator,
            "rejection_rate": self.rejected_rules / denominator,
            "false_accept_rate": self.evaluation.false_accepts / denominator,
            "false_rejection_rate": self.evaluation.false_rejections / denominator,
            "semantic_match_rate": self.evaluation.semantic_matches / denominator,
        }
