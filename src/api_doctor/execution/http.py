"""Restricted HTTP execution for legacy M0 and normalized OpenAPI cases."""

from __future__ import annotations

import json
import logging
import socket
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from api_doctor.execution.response_validation import validate_response
from api_doctor.execution.safety import ExecutionPolicy, SafetyViolation
from api_doctor.observability import redact
from api_doctor.openapi.models import Operation
from api_doctor.testspec.models import CompiledTestCase, TestResult


logger = logging.getLogger(__name__)


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, policy: ExecutionPolicy) -> None:
        self.policy = policy
        self.redirects = 0
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > self.policy.max_redirects:
            raise SafetyViolation("redirect limit exceeded")
        self.policy.ensure_same_origin(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(case: CompiledTestCase, target_url: str) -> Request:
    headers = {str(key): str(value) for key, value in case.request.headers.items()}
    if case.request.media_type:
        headers.setdefault("Content-Type", case.request.media_type)
    if case.request.cookies:
        headers["Cookie"] = "; ".join(
            f"{key}={value}" for key, value in case.request.cookies.items()
        )
    data = (
        json.dumps(case.request.body).encode("utf-8")
        if case.request.body is not None
        else None
    )
    return Request(target_url, data=data, headers=headers, method=case.method)


def execute_openapi_case(
    case: CompiledTestCase,
    operation: Operation,
    policy: ExecutionPolicy,
) -> TestResult:
    expected = case.assertions[0].allowed
    try:
        target_url = policy.resolve_url(case)
    except SafetyViolation as error:
        return TestResult(
            case_id=case.case_id,
            outcome="blocked",
            actual_status=None,
            expected_statuses=expected,
            error=str(error),
        )

    request = _request(case, target_url)
    opener = build_opener(_SameOriginRedirectHandler(policy))
    started = perf_counter()
    try:
        try:
            response = opener.open(request, timeout=policy.timeout_seconds)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            raw_body = response.read(policy.max_response_bytes + 1)
    except (URLError, TimeoutError, socket.timeout, OSError, SafetyViolation) as error:
        return TestResult(
            case_id=case.case_id,
            outcome="blocked" if isinstance(error, SafetyViolation) else "error",
            actual_status=None,
            expected_statuses=expected,
            request_url=target_url,
            duration_ms=round((perf_counter() - started) * 1000),
            error=str(error),
        )
    duration_ms = round((perf_counter() - started) * 1000)
    if len(raw_body) > policy.max_response_bytes:
        return TestResult(
            case_id=case.case_id,
            outcome="error",
            actual_status=status,
            expected_statuses=expected,
            request_url=target_url,
            duration_ms=duration_ms,
            error=f"response exceeded {policy.max_response_bytes} bytes",
        )
    try:
        body = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        body = raw_body.decode("utf-8", errors="replace")

    violations = []
    if status not in expected:
        violations.append(f"status {status} is not in expected set {expected}")
    violations.extend(
        validate_response(
            operation,
            status=status,
            headers=response_headers,
            body=body,
        )
    )
    return TestResult(
        case_id=case.case_id,
        outcome="failed" if violations else "passed",
        actual_status=status,
        expected_statuses=expected,
        response_body=body,
        response_headers=redact(response_headers),
        request_url=target_url,
        duration_ms=duration_ms,
        violations=violations,
    )


def execute_case(case: CompiledTestCase, base_url: str) -> TestResult:
    """Legacy status-only M0 executor retained for benchmark comparability."""
    policy = ExecutionPolicy(base_url=base_url)
    target_url = policy.resolve_url(case)
    request = _request(case, target_url)
    logger.debug("[HTTP] request case=%s method=%s url=%s", case.case_id, case.method, target_url)
    started = perf_counter()
    try:
        with urlopen(request, timeout=policy.timeout_seconds) as response:
            status = response.status
            raw_body = response.read(policy.max_response_bytes + 1)
    except HTTPError as error:
        status = error.code
        raw_body = error.read(policy.max_response_bytes + 1)
    duration_ms = round((perf_counter() - started) * 1000)
    if len(raw_body) > policy.max_response_bytes:
        return TestResult(
            case_id=case.case_id,
            outcome="error",
            actual_status=status,
            expected_statuses=case.assertions[0].allowed,
            error=f"response exceeded {policy.max_response_bytes} bytes",
        )
    try:
        body = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        body = raw_body.decode("utf-8", errors="replace")
    allowed = case.assertions[0].allowed
    result = TestResult(
        case_id=case.case_id,
        outcome="passed" if status in allowed else "failed",
        actual_status=status,
        expected_statuses=allowed,
        response_body=body,
        request_url=target_url,
        duration_ms=duration_ms,
    )
    logger.debug(
        "[HTTP] response case=%s status=%d expected=%s outcome=%s duration_ms=%d",
        case.case_id,
        status,
        allowed,
        result.outcome,
        duration_ms,
    )
    return result
