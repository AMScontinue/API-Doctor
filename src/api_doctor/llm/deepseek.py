"""DeepSeek implementation of semantic TestSpec extraction."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from pydantic import TypeAdapter

from api_doctor.testspec.models import Operation, RuleRecord, SemanticTestCandidate


class DeepSeekExtractor:
    def __init__(self) -> None:
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not self._api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for --mode deepseek")
        self._model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self._url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")

    def extract(self, rule: RuleRecord, operations: dict[str, Operation]) -> SemanticTestCandidate:
        prompt = {
            "task": "Convert one business rule into the supported TestSpec candidate. Return not_compilable when the rule cannot be represented. Never invent operation IDs or fields.",
            "supported_mutations": ["set_value", "omit_field", "boundary", "field_relation"],
            "rule": rule.model_dump(mode="json"),
            "operations": [item.model_dump(mode="json") for item in operations.values()],
            "output_json_schema": SemanticTestCandidate.model_json_schema(),
        }
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
        with urlopen(request, timeout=60) as response:
            raw = json.loads(response.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        return TypeAdapter(SemanticTestCandidate).validate_json(content)
