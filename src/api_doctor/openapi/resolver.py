"""Resolve references within one OpenAPI document."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from api_doctor.openapi.models import ParseWarning


class OpenAPIReferenceError(ValueError):
    """Raised when a reference cannot be resolved safely."""


def _decode_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


class LocalRefResolver:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.warnings: list[ParseWarning] = []

    def resolve(self, value: Any, *, location: str = "#") -> Any:
        return self._resolve(value, location=location, stack=())

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise OpenAPIReferenceError(
                f"external reference is not supported: {ref!r}"
            )
        current: Any = self.document
        for encoded in ref[2:].split("/"):
            token = _decode_pointer_token(encoded)
            try:
                current = current[int(token)] if isinstance(current, list) else current[token]
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise OpenAPIReferenceError(f"reference does not exist: {ref!r}") from exc
        return current

    def _resolve(self, value: Any, *, location: str, stack: tuple[str, ...]) -> Any:
        if isinstance(value, list):
            return [
                self._resolve(item, location=f"{location}/{index}", stack=stack)
                for index, item in enumerate(value)
            ]
        if not isinstance(value, dict):
            return value

        ref = value.get("$ref")
        if isinstance(ref, str):
            if ref in stack:
                self.warnings.append(
                    ParseWarning(
                        code="recursive_ref",
                        location=location,
                        message=f"recursive reference retained without further expansion: {ref}",
                    )
                )
                return deepcopy(value)
            target = deepcopy(self._lookup(ref))
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            if siblings:
                if not isinstance(target, dict):
                    raise OpenAPIReferenceError(f"reference with siblings is not an object: {ref!r}")
                target.update(siblings)
            return self._resolve(target, location=location, stack=(*stack, ref))

        return {
            key: self._resolve(item, location=f"{location}/{key}", stack=stack)
            for key, item in value.items()
        }
