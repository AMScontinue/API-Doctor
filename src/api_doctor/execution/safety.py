"""Deterministic request safety policy for the baseline executor."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode, urljoin, urlparse

from api_doctor.testspec.models import CompiledTestCase


class SafetyViolation(ValueError):
    """A compiled request violates an execution boundary."""


@dataclass(frozen=True)
class ExecutionPolicy:
    base_url: str
    approve_delete: bool = False
    timeout_seconds: float = 5.0
    max_response_bytes: int = 1_000_000
    max_redirects: int = 3

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SafetyViolation("base URL must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SafetyViolation("base URL cannot contain credentials, query, or fragment")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0 or self.max_redirects < 0:
            raise SafetyViolation("execution limits must be positive")

    @property
    def origin(self) -> tuple[str, str | None, int | None]:
        parsed = urlparse(self.base_url)
        return parsed.scheme, parsed.hostname, self._effective_port(parsed.scheme, parsed.port)

    @staticmethod
    def _effective_port(scheme: str, port: int | None) -> int | None:
        if port is not None:
            return port
        return 80 if scheme == "http" else 443 if scheme == "https" else None

    def ensure_same_origin(self, url: str) -> None:
        target = urlparse(url)
        target_origin = (
            target.scheme,
            target.hostname,
            self._effective_port(target.scheme, target.port),
        )
        if target_origin != self.origin:
            raise SafetyViolation("request or redirect escaped configured base URL origin")

    def resolve_url(self, case: CompiledTestCase) -> str:
        method = case.request.method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise SafetyViolation(f"HTTP method {method!r} is not allowed")
        if method == "DELETE" and not self.approve_delete:
            raise SafetyViolation("DELETE requires explicit --approve-delete")

        rendered_path = case.request.path_template
        for name, value in case.request.path_parameters.items():
            rendered_path = rendered_path.replace("{" + name + "}", quote(str(value), safe=""))
        if "{" in rendered_path or "}" in rendered_path:
            raise SafetyViolation("compiled request has unresolved path parameters")
        target = urljoin(self.base_url.rstrip("/") + "/", rendered_path.lstrip("/"))
        if case.request.query_parameters:
            target += "?" + urlencode(case.request.query_parameters, doseq=True)
        self.ensure_same_origin(target)
        return target
