"""Small logging helpers with conservative field-name redaction."""

from __future__ import annotations

import json
from typing import Any


SENSITIVE_PARTS = ("authorization", "cookie", "api_key", "apikey", "password", "secret", "token")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        path = str(value.get("path", "")).lower()
        redact_value = any(part in path for part in SENSITIVE_PARTS)
        return {
            key: "***REDACTED***"
            if any(part in key.lower() for part in SENSITIVE_PARTS) or (key == "value" and redact_value)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def json_for_log(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True, default=str)
