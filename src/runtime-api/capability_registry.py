"""Provider-neutral, ephemeral read-capability registry.

Adapters inject public descriptors and read-only callables at daemon
composition time.  This module deliberately knows nothing about providers or
skills, and it never receives a RequestStore: discovery/read traffic must not
become durable request state.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Mapping
from typing import Any, Optional

from protocol import ProtocolError


DEFAULT_READ_TIMEOUT_S = 10.0
MAX_READ_TIMEOUT_S = 10.0
MAX_READ_RESULT_BYTES = 192 * 1024
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_READ_LIMIT = 100
MAX_JSON_DEPTH = 64

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_AVAILABILITY = frozenset({
    "ready",
    "authorization_required",
    "permission_limited",
    "unavailable",
})
_DESCRIPTOR_KEYS = frozenset({
    "id",
    "version",
    "availability",
    "operations",
    "description",
    "identity",
    "constraints",
    "setup",
    "metadata",
})
_READ_KEYS = frozenset({"capabilityId", "operation", "resource", "cursor", "limit"})


def _name(value: Any, field: str) -> str:
    if (not isinstance(value, str) or len(value) > 120
            or _NAME_RE.fullmatch(value) is None):
        raise ValueError(f"{field} must be a provider-neutral capability name")
    return value


def _walk_json(value: Any, field: str, depth: int = 0) -> None:
    """Reject values that JSON can coerce silently (non-string keys, NaN)."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError(
            f"{field} exceeds the {MAX_JSON_DEPTH}-level JSON depth limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must contain finite JSON numbers")
        return
    if isinstance(value, list):
        for item in value:
            _walk_json(item, field, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} must contain only string object keys")
            _walk_json(item, field, depth + 1)
        return
    raise ValueError(f"{field} must be JSON-compatible")


def _json_copy(value: Any, field: str, max_bytes: int) -> tuple[Any, int]:
    _walk_json(value, field)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON-compatible") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field} exceeds the {max_bytes}-byte limit")
    return json.loads(encoded.decode("utf-8")), len(encoded)


class EphemeralCapabilityRegistry:
    """Validated descriptors paired with bounded, read-only callables.

    A reader is called as ``reader(params)`` where params contains the
    validated ``capabilityId``, ``operation``, optional ``resource`` and
    ``cursor`` objects, and a bounded ``limit``. It must return a JSON object.
    """

    def __init__(
        self,
        descriptors: Optional[Mapping[str, Mapping[str, Any]]] = None,
        readers: Optional[
            Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]
        ] = None,
        *,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        max_result_bytes: int = MAX_READ_RESULT_BYTES,
    ):
        if (isinstance(read_timeout_s, bool) or not isinstance(read_timeout_s, (int, float))
                or not math.isfinite(float(read_timeout_s)) or read_timeout_s <= 0
                or read_timeout_s > MAX_READ_TIMEOUT_S):
            raise ValueError(
                f"read_timeout_s must be a positive number no greater than {MAX_READ_TIMEOUT_S}")
        if (isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int)
                or max_result_bytes < 1024 or max_result_bytes > MAX_READ_RESULT_BYTES):
            raise ValueError(
                f"max_result_bytes must be an integer from 1024 to {MAX_READ_RESULT_BYTES}")

        self.read_timeout_s = float(read_timeout_s)
        self.max_result_bytes = max_result_bytes
        self._descriptors: dict[str, dict[str, Any]] = {}

        if not isinstance(descriptors, Mapping) and descriptors is not None:
            raise ValueError("descriptors must be a mapping")
        if not isinstance(readers, Mapping) and readers is not None:
            raise ValueError("readers must be a mapping")
        self._readers = dict(readers or {})

        for capability_id, descriptor in (descriptors or {}).items():
            capability_id = _name(capability_id, "descriptor key")
            self._descriptors[capability_id] = self._validate_descriptor(
                capability_id, descriptor)

        for capability_id, reader in self._readers.items():
            _name(capability_id, "reader key")
            if capability_id not in self._descriptors:
                raise ValueError(f"reader {capability_id!r} has no descriptor")
            if not callable(reader):
                raise ValueError(f"reader {capability_id!r} must be callable")

        # Bound discovery as well as individual reads. A registry that cannot
        # be returned safely is rejected at composition time, not mid-request.
        _json_copy({"capabilities": list(self._descriptors.values())},
                   "capability descriptors", self.max_result_bytes)

    @staticmethod
    def _validate_descriptor(capability_id: str,
                             descriptor: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(descriptor, Mapping):
            raise ValueError(f"descriptor {capability_id!r} must be an object")
        if any(not isinstance(key, str) for key in descriptor):
            raise ValueError(f"descriptor {capability_id!r} fields must be strings")
        unknown = set(descriptor) - _DESCRIPTOR_KEYS
        if unknown:
            raise ValueError(
                f"descriptor {capability_id!r} has unknown field(s): {', '.join(sorted(unknown))}")
        if descriptor.get("id") != capability_id:
            raise ValueError(f"descriptor {capability_id!r} id must match its registry key")
        version = descriptor.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError(f"descriptor {capability_id!r} version must be a positive integer")
        availability = descriptor.get("availability")
        if not isinstance(availability, str) or availability not in _AVAILABILITY:
            raise ValueError(
                f"descriptor {capability_id!r} availability must be one of "
                f"{', '.join(sorted(_AVAILABILITY))}")
        operations = descriptor.get("operations")
        if not isinstance(operations, list) or not operations or len(operations) > 32:
            raise ValueError(
                f"descriptor {capability_id!r} operations must be a non-empty list of at most 32 names")
        clean_operations = [_name(op, "operation") for op in operations]
        if len(clean_operations) != len(set(clean_operations)):
            raise ValueError(f"descriptor {capability_id!r} operations must be unique")
        description = descriptor.get("description")
        if description is not None and (not isinstance(description, str)
                                        or len(description) > 500):
            raise ValueError(f"descriptor {capability_id!r} description is too long")
        for field in ("identity", "constraints", "setup", "metadata"):
            if field in descriptor and not isinstance(descriptor[field], Mapping):
                raise ValueError(f"descriptor {capability_id!r} {field} must be an object")

        clean, _ = _json_copy(dict(descriptor), f"descriptor {capability_id!r}",
                              MAX_DESCRIPTOR_BYTES)
        return clean

    def list(self, params: dict[str, Any]) -> dict[str, Any]:
        if params:
            raise ProtocolError(-32602, "capability.list does not accept params")
        # Return a fresh copy: callers cannot mutate the registry's public
        # descriptor state in an in-process composition.
        result, _ = _json_copy(
            {"capabilities": [self._descriptors[key]
                              for key in sorted(self._descriptors)]},
            "capability.list result", self.max_result_bytes)
        return result

    async def read(self, params: dict[str, Any]) -> dict[str, Any]:
        unknown = set(params) - _READ_KEYS
        if unknown:
            raise ProtocolError(
                -32602, f"unknown capability.read param(s): {', '.join(sorted(unknown))}")
        try:
            capability_id = _name(params.get("capabilityId"), "capabilityId")
            operation = _name(params.get("operation"), "operation")
        except ValueError as exc:
            raise ProtocolError(-32602, str(exc)) from exc

        descriptor = self._descriptors.get(capability_id)
        if descriptor is None:
            raise ProtocolError(-32602, f"unknown capabilityId: {capability_id!r}")
        if operation not in descriptor["operations"]:
            raise ProtocolError(
                -32602, f"operation {operation!r} is not declared by {capability_id!r}")
        reader = self._readers.get(capability_id)
        if reader is None:
            raise ProtocolError(-32602, f"capability {capability_id!r} is not readable")

        normalized: dict[str, Any] = {
            "capabilityId": capability_id,
            "operation": operation,
        }
        for field in ("resource", "cursor"):
            if field in params:
                value = params[field]
                if not isinstance(value, dict):
                    raise ProtocolError(-32602, f"{field} must be an object")
                try:
                    normalized[field], _ = _json_copy(
                        value, field, MAX_DESCRIPTOR_BYTES)
                except ValueError as exc:
                    raise ProtocolError(-32602, str(exc)) from exc

        limit = params.get("limit", MAX_READ_LIMIT)
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or limit < 1 or limit > MAX_READ_LIMIT):
            raise ProtocolError(-32602, f"limit must be an integer from 1 to {MAX_READ_LIMIT}")
        normalized["limit"] = limit

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(reader, normalized), timeout=self.read_timeout_s)
        except asyncio.TimeoutError as exc:
            raise ProtocolError(-32000, "capability read timed out") from exc
        except Exception as exc:  # provider errors must not leak secrets
            raise ProtocolError(-32000, "capability read failed") from exc

        if not isinstance(result, dict):
            raise ProtocolError(-32000, "capability reader returned a non-object result")
        try:
            clean, _ = _json_copy(result, "capability read result", self.max_result_bytes)
        except ValueError as exc:
            raise ProtocolError(-32000, str(exc)) from exc
        return clean


def compose_capability_registry(provider_factories=()) -> EphemeralCapabilityRegistry:
    """Compose injected provider factories without discovering provider code."""
    if provider_factories is None:
        provider_factories = ()
    try:
        factories = tuple(provider_factories)
    except TypeError as exc:
        raise ValueError("provider_factories must be iterable") from exc

    descriptors: dict[str, Mapping[str, Any]] = {}
    readers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
    for factory in factories:
        if not callable(factory):
            raise ValueError("every provider factory must be callable")
        try:
            supplied = factory()
        except Exception:
            raise ValueError("provider factory failed") from None
        if not isinstance(supplied, tuple) or len(supplied) != 2:
            raise ValueError("provider factory must return (descriptors, readers)")
        provider_descriptors, provider_readers = supplied
        if not isinstance(provider_descriptors, Mapping):
            raise ValueError("provider descriptors must be a mapping")
        if not isinstance(provider_readers, Mapping):
            raise ValueError("provider readers must be a mapping")
        if not set(provider_readers).issubset(provider_descriptors):
            raise ValueError("provider reader has no descriptor in the same factory")
        existing_ids = set(descriptors).union(readers)
        provider_ids = set(provider_descriptors).union(provider_readers)
        duplicates = existing_ids.intersection(provider_ids)
        if duplicates:
            names = ", ".join(sorted(str(item) for item in duplicates))
            raise ValueError(f"duplicate capability provider: {names}")
        descriptors.update(provider_descriptors)
        readers.update(provider_readers)
    return EphemeralCapabilityRegistry(descriptors, readers)
