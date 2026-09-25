"""Restricted HTTP executor for compiled M0 cases."""

from __future__ import annotations

import json
import logging
from time import perf_counter
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from api_doctor.testspec.models import CompiledTestCase, TestResult


logger = logging.getLogger(__name__)


def execute_case(case: CompiledTestCase, base_url: str) -> TestResult:
    base = urlparse(base_url)
    target = urlparse(urljoin(base_url.rstrip("/") + "/", case.path.lstrip("/")))
    if (target.scheme, target.hostname, target.port) != (base.scheme, base.hostname, base.port):
        raise ValueError("compiled request escaped configured base URL")
    request = Request(
        target.geturl(),
        data=json.dumps(case.body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=case.method,
    )
    status: int
    body: object
    logger.debug("[HTTP] request case=%s method=%s url=%s", case.case_id, case.method, target.geturl())
    started = perf_counter()
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            raw_body = response.read(1_000_001)
    except HTTPError as error:
        status = error.code
        raw_body = error.read(1_000_001)
    duration_ms = round((perf_counter() - started) * 1000)
    if len(raw_body) > 1_000_000:
        return TestResult(
            case_id=case.case_id,
            outcome="error",
            actual_status=status,
            expected_statuses=case.assertions[0].allowed,
            error="response exceeded 1 MB",
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
