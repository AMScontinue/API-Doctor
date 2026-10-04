"""Load and normalize OpenAPI 3.x YAML or JSON documents."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from api_doctor.openapi.models import (
    Operation,
    OperationCatalog,
    Parameter,
    ParseWarning,
    RequestBody,
    ResponseSpec,
    SecurityScheme,
)
from api_doctor.openapi.resolver import LocalRefResolver


HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


class OpenAPIParseError(ValueError):
    """Raised when a document cannot become a safe operation catalog."""


def _read_document(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise OpenAPIParseError(f"cannot read OpenAPI document {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise OpenAPIParseError("OpenAPI document root must be an object")
    return raw


def _generated_operation_id(method: str, path: str) -> str:
    path_part = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "root"
    return f"generated_{method.lower()}_{path_part}"


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _media(content: Any, location: str, warnings: list[ParseWarning]) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(content, dict) or not content:
        return None, None
    if "application/json" in content:
        selected = "application/json"
    else:
        json_like = next((name for name in content if name.endswith("+json")), None)
        selected = json_like or next(iter(content))
        warnings.append(ParseWarning(
            code="non_json_media_type",
            location=location,
            message=f"using {selected!r}; MVP execution primarily supports JSON",
        ))
    media = content[selected]
    schema = media.get("schema", {}) if isinstance(media, dict) else {}
    return selected, schema if isinstance(schema, dict) else {}


def _parameter(raw: dict[str, Any], location: str) -> Parameter:
    where = raw.get("in")
    if where not in {"path", "query", "header", "cookie"}:
        raise OpenAPIParseError(f"unsupported parameter location at {location}: {where!r}")
    if not isinstance(raw.get("name"), str):
        raise OpenAPIParseError(f"parameter name is missing at {location}")
    return Parameter(
        name=raw["name"],
        location=where,
        required=True if where == "path" else bool(raw.get("required", False)),
        description=raw.get("description"),
        json_schema=raw.get("schema") if isinstance(raw.get("schema"), dict) else {},
        example=raw.get("example"),
        style=raw.get("style"),
        explode=raw.get("explode"),
        allow_reserved=bool(raw.get("allowReserved", False)),
    )


def _merge_parameters(path_items: Any, operation_items: Any, location: str) -> list[Parameter]:
    merged: dict[tuple[str, str], Parameter] = {}
    for scope, items in (("path", path_items), ("operation", operation_items)):
        if items is None:
            continue
        if not isinstance(items, list):
            raise OpenAPIParseError(f"parameters must be an array at {location}/{scope}")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise OpenAPIParseError(f"parameter must be an object at {location}/{scope}/{index}")
            parameter = _parameter(item, f"{location}/{scope}/{index}")
            merged[(parameter.name, parameter.location)] = parameter
    return list(merged.values())


def _security_schemes(raw: Any) -> dict[str, SecurityScheme]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, SecurityScheme] = {}
    for name, scheme in raw.items():
        if not isinstance(scheme, dict) or not isinstance(scheme.get("type"), str):
            continue
        result[name] = SecurityScheme(
            name=name,
            type=scheme["type"],
            scheme=scheme.get("scheme"),
            bearer_format=scheme.get("bearerFormat"),
            parameter_name=scheme.get("name"),
            location=scheme.get("in"),
            flows=scheme.get("flows") if isinstance(scheme.get("flows"), dict) else None,
        )
    return result


def load_openapi(path: Path) -> OperationCatalog:
    """Parse a file and return the stable catalog consumed by later stages."""
    source = path.resolve()
    document = _read_document(source)
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        raise OpenAPIParseError(f"only OpenAPI 3.x is supported, got {version!r}")

    resolver = LocalRefResolver(document)
    resolved = resolver.resolve(document)
    warnings = resolver.warnings
    paths = resolved.get("paths")
    if not isinstance(paths, dict):
        raise OpenAPIParseError("OpenAPI document must contain a paths object")

    operations: dict[str, Operation] = {}
    root_security = resolved.get("security")
    for api_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            warnings.append(ParseWarning(code="invalid_path_item", location=f"#/paths/{api_path}", message="path item is not an object"))
            continue
        for method in HTTP_METHODS:
            raw = path_item.get(method)
            if raw is None:
                continue
            pointer = f"#/paths/{_pointer_token(str(api_path))}/{method}"
            if not isinstance(raw, dict):
                raise OpenAPIParseError(f"operation must be an object at {pointer}")
            operation_id = raw.get("operationId")
            if not isinstance(operation_id, str) or not operation_id.strip():
                operation_id = _generated_operation_id(method, str(api_path))
                warnings.append(ParseWarning(code="generated_operation_id", location=pointer, message=f"missing operationId; generated {operation_id!r}"))
            if operation_id in operations:
                raise OpenAPIParseError(f"duplicate operationId {operation_id!r} at {pointer}")

            request_body = None
            request = raw.get("requestBody")
            if isinstance(request, dict):
                media_type, schema = _media(request.get("content"), f"{pointer}/requestBody/content", warnings)
                if media_type:
                    request_body = RequestBody(required=bool(request.get("required", False)), media_type=media_type, json_schema=schema or {})

            responses: list[ResponseSpec] = []
            raw_responses = raw.get("responses", {})
            if not isinstance(raw_responses, dict):
                raise OpenAPIParseError(f"responses must be an object at {pointer}/responses")
            for status, response in raw_responses.items():
                if not isinstance(response, dict):
                    continue
                media_type, schema = _media(response.get("content"), f"{pointer}/responses/{status}/content", warnings)
                responses.append(ResponseSpec(status_code=str(status), description=str(response.get("description", "")), media_type=media_type, json_schema=schema))

            security = raw["security"] if "security" in raw else root_security
            operations[operation_id] = Operation(
                operation_id=operation_id,
                method=method.upper(),
                path=str(api_path),
                summary=raw.get("summary"),
                description=raw.get("description"),
                tags=raw.get("tags") if isinstance(raw.get("tags"), list) else [],
                parameters=_merge_parameters(path_item.get("parameters"), raw.get("parameters"), pointer),
                request_body=request_body,
                responses=responses,
                security=security if isinstance(security, list) else None,
                source_pointer=pointer,
            )

    info = resolved.get("info") if isinstance(resolved.get("info"), dict) else {}
    fingerprint = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    components = resolved.get("components") if isinstance(resolved.get("components"), dict) else {}
    return OperationCatalog(
        title=str(info.get("title", source.stem)),
        version=str(info.get("version", "unknown")),
        openapi_version=version,
        source=str(source),
        fingerprint=fingerprint,
        operations=operations,
        security_schemes=_security_schemes(components.get("securitySchemes")),
        warnings=warnings,
    )
