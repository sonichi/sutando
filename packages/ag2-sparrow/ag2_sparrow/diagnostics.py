"""The process's one secret registry, and the credential owner's masking bound to it for the sinks.

Sinks take structured safe fields only: an exception's type (with its reason's type, an errno name,
an HTTP status and where it was raised), the redacted gateway identity, and text the code wrote. Never an exception's
own text, a response body, or gateway-supplied free text. Behaviour code may keep those for control
flow; it never hands them to a sink. Every rule is gateway_credentials' (the one implementation);
this module only holds the registry the log and status sinks share and calls the owner with it.
"""
from __future__ import annotations

from typing import Any, Callable

from . import gateway_credentials

WITHHELD = gateway_credentials.WITHHELD
WITHHELD_TEXT = gateway_credentials.WITHHELD_TEXT
BARE_SECRET_MIN = gateway_credentials.BARE_SECRET_MIN
REGISTRY = gateway_credentials.SecretRegistry()  # this process's secrets, for every sink


def register_secret(value: Any, min_len: int = 1) -> None:
    return REGISTRY.register(value, min_len)


def register_gateway_url(url: str) -> None:
    return REGISTRY.register_gateway_url(url)


def contains_secret(text: str) -> bool:
    return REGISTRY.contains(text)


def holds_or_is_piece(value: str) -> bool:
    return REGISTRY.holds_or_is_piece(value)


def scrub(text: str) -> str:
    return REGISTRY.scrub(text)


def code_field(value: Any, gateway_url: str = "") -> "str | None":
    return gateway_credentials.code_field(value, REGISTRY, gateway_url)


def version_field(value: Any) -> "str | None":
    return gateway_credentials.version_field(value, REGISTRY)


def revision_field(value: Any, gateway_url: str = "") -> "str | None":
    return gateway_credentials.revision_field(value, REGISTRY, gateway_url)


def describe(exc: BaseException) -> str:
    return gateway_credentials.describe(exc, REGISTRY)


def safe_status(value: Any, net: Callable[[str], str]) -> Any:
    return gateway_credentials.safe_status(value, net, REGISTRY)
