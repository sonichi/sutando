"""Canonical identity-attribution claim contract and append-only writer."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

SCHEMA_VERSION = 1
MAX_CLAIM_BYTES = 16 * 1024
MAX_RECEIPTS = 8
PREDICATES = frozenset({
    "uses_account", "performer_kind_policy", "performed_by", "retracts",
})
BASES = frozenset({
    "provider_auth_observed", "owner_asserted", "owner_policy", "runtime_receipt",
})
_PROVIDERS = frozenset({"github", "discord", "ag2space"})
_PRINCIPAL_RE = re.compile(
    r"^(?:agent|human):[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CLAIM_RE = re.compile(r"^claim:[0-9a-f]{64}$")
_EVENT_RE = re.compile(r"^event:([a-z][a-z0-9_-]{0,31}):[0-9a-f]{64}$")
_ACCOUNT_RE = re.compile(r"^account:([a-z][a-z0-9_-]{0,31}):([^\s:][^\s]*)$")


class AttributionError(ValueError):
    pass


class AttributionStoreError(RuntimeError):
    pass


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise AttributionError(f"{field} must be a non-empty string up to {limit} characters")
    return value


def _timestamp(value: Any, field: str = "asserted_at") -> str:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttributionError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise AttributionError(f"{field} must include a timezone")
    return text


def is_canonical_agent_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("agent:") and bool(_PRINCIPAL_RE.fullmatch(value))


def canonical_account_id(provider: str, provider_id: str) -> str:
    provider = _text(provider, "provider", 32).casefold()
    if provider not in _PROVIDERS:
        raise AttributionError(f"unsupported provider: {provider}")
    provider_id = _text(provider_id, "provider_id", 300)
    return f"account:{provider}:{quote(provider_id, safe='-._~')}"


def claim_id_for(dedupe_key: str) -> str:
    key = _text(dedupe_key, "dedupe_key", 500)
    return f"claim:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def normalize_receipt(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise AttributionError("receipt must be an object")
    allowed = {"provider", "account_id", "resource_id", "object_type", "object_id"}
    unknown = set(value) - allowed
    if unknown:
        raise AttributionError(f"receipt has unknown field(s): {', '.join(sorted(unknown))}")
    provider = _text(value.get("provider"), "receipt.provider", 32).casefold()
    if provider not in _PROVIDERS:
        raise AttributionError(f"unsupported receipt provider: {provider}")
    account = _text(value.get("account_id"), "receipt.account_id", 500)
    match = _ACCOUNT_RE.fullmatch(account)
    if match is None or match.group(1) != provider:
        raise AttributionError("receipt.account_id must be canonical and match receipt.provider")
    return {
        "provider": provider,
        "account_id": account,
        "resource_id": _text(value.get("resource_id"), "receipt.resource_id", 300),
        "object_type": _text(value.get("object_type"), "receipt.object_type", 100),
        "object_id": _text(value.get("object_id"), "receipt.object_id", 500),
    }


def canonical_event_id(receipt: Mapping[str, Any]) -> str:
    clean = normalize_receipt(receipt)
    identity = {key: clean[key] for key in (
        "provider", "resource_id", "object_type", "object_id",
    )}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"event:{clean['provider']}:{hashlib.sha256(encoded).hexdigest()}"


def _envelope(
    *, dedupe_key: str, predicate: str, subject: str, object_: str,
    basis: str, asserted_at: str, author: str, scope: Optional[dict] = None,
    evidence: Optional[dict] = None,
) -> dict[str, Any]:
    claim = {
        "schema_version": SCHEMA_VERSION,
        "id": claim_id_for(dedupe_key),
        "dedupe_key": dedupe_key,
        "predicate": predicate,
        "subject": subject,
        "object": object_,
        "basis": basis,
        "asserted_at": asserted_at,
        "author": author,
    }
    if scope is not None:
        claim["scope"] = scope
    if evidence is not None:
        claim["evidence"] = evidence
    return validate_claim(claim)


def performed_by_claim(
    *, actor_id: str, receipt: Mapping[str, Any], runtime_request_id: str,
    asserted_at: str,
) -> dict[str, Any]:
    if not is_canonical_agent_id(actor_id):
        raise AttributionError("exact runtime attribution requires a canonical agent ID")
    clean = normalize_receipt(receipt)
    request_id = _text(runtime_request_id, "runtime_request_id", 160)
    evidence = {**clean, "runtime_request_id": request_id}
    return _envelope(
        dedupe_key=f"runtime-receipt:{request_id}:{canonical_event_id(clean)}",
        predicate="performed_by",
        subject=canonical_event_id(clean),
        object_=actor_id,
        basis="runtime_receipt",
        asserted_at=asserted_at,
        author=actor_id,
        evidence=evidence,
    )


def uses_account_claim(
    *, principal_id: str, account_id: str, basis: str, asserted_at: str,
    author: str, dedupe_key: str,
) -> dict[str, Any]:
    return _envelope(
        dedupe_key=dedupe_key, predicate="uses_account", subject=principal_id,
        object_=account_id, basis=basis, asserted_at=asserted_at, author=author,
    )


def performer_kind_policy_claim(
    *, account_id: str, performer_kind: str, scope: Mapping[str, Any],
    asserted_at: str, author: str, dedupe_key: str,
) -> dict[str, Any]:
    return _envelope(
        dedupe_key=dedupe_key, predicate="performer_kind_policy", subject=account_id,
        object_=performer_kind, basis="owner_policy", asserted_at=asserted_at,
        author=author, scope=dict(scope),
    )


def retraction_claim(
    *, target_claim_id: str, asserted_at: str, author: str, dedupe_key: str,
) -> dict[str, Any]:
    return _envelope(
        dedupe_key=dedupe_key, predicate="retracts", subject=claim_id_for(dedupe_key),
        object_=target_claim_id, basis="owner_asserted", asserted_at=asserted_at,
        author=author,
    )


def _scope(value: Any, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AttributionError("performer_kind_policy requires an object scope")
    allowed = {
        "provider", "account_ids", "resource_ids", "object_types",
        "exclude_resource_ids", "exclude_object_types", "not_before", "not_after",
    }
    unknown = set(value) - allowed
    if unknown:
        raise AttributionError(f"scope has unknown field(s): {', '.join(sorted(unknown))}")
    provider = _text(value.get("provider"), "scope.provider", 32).casefold()
    account_ids = value.get("account_ids")
    if provider not in _PROVIDERS or not isinstance(account_ids, list) or not account_ids:
        raise AttributionError("scope requires a supported provider and non-empty account_ids")
    clean_accounts = []
    for account in account_ids:
        account = _text(account, "scope.account_ids[]", 500)
        match = _ACCOUNT_RE.fullmatch(account)
        if match is None or match.group(1) != provider:
            raise AttributionError("scope account IDs must be canonical and match provider")
        clean_accounts.append(account)
    if clean_accounts != [subject]:
        raise AttributionError("v1 policy scope must contain only its subject account")
    clean: dict[str, Any] = {"provider": provider, "account_ids": clean_accounts}
    for field, limit in (
        ("resource_ids", 300), ("object_types", 100),
        ("exclude_resource_ids", 300), ("exclude_object_types", 100),
    ):
        values = value.get(field, [])
        if not isinstance(values, list) or len(values) > 100:
            raise AttributionError(f"scope.{field} must be a list of at most 100 strings")
        clean[field] = [_text(item, f"scope.{field}[]", limit) for item in values]
    for field in ("not_before", "not_after"):
        if value.get(field) is not None:
            clean[field] = _timestamp(value[field], f"scope.{field}")
    return clean


def validate_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AttributionError("claim must be an object")
    allowed = {
        "schema_version", "id", "dedupe_key", "predicate", "subject", "object",
        "basis", "asserted_at", "author", "scope", "evidence",
    }
    unknown = set(value) - allowed
    if unknown:
        raise AttributionError(f"claim has unknown field(s): {', '.join(sorted(unknown))}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise AttributionError(f"schema_version must be {SCHEMA_VERSION}")
    dedupe_key = _text(value.get("dedupe_key"), "dedupe_key", 500)
    claim_id = _text(value.get("id"), "id", 80)
    if not _CLAIM_RE.fullmatch(claim_id) or claim_id != claim_id_for(dedupe_key):
        raise AttributionError("claim id must be derived from dedupe_key")
    predicate = _text(value.get("predicate"), "predicate", 40)
    basis = _text(value.get("basis"), "basis", 40)
    if predicate not in PREDICATES or basis not in BASES:
        raise AttributionError("unsupported predicate or basis")
    subject = _text(value.get("subject"), "subject", 500)
    object_ = _text(value.get("object"), "object", 500)
    author = _text(value.get("author"), "author", 160)
    if not _PRINCIPAL_RE.fullmatch(author):
        raise AttributionError("author must be a canonical human or agent principal")
    clean: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "id": claim_id, "dedupe_key": dedupe_key,
        "predicate": predicate, "subject": subject, "object": object_, "basis": basis,
        "asserted_at": _timestamp(value.get("asserted_at")), "author": author,
    }
    if predicate == "uses_account":
        if not _PRINCIPAL_RE.fullmatch(subject) or _ACCOUNT_RE.fullmatch(object_) is None:
            raise AttributionError("uses_account requires principal subject and account object")
        if basis not in {"provider_auth_observed", "owner_asserted"}:
            raise AttributionError("uses_account has an invalid basis")
        if basis == "owner_asserted" and not author.startswith("human:"):
            raise AttributionError("owner_asserted claims require a human author")
    elif predicate == "performer_kind_policy":
        if _ACCOUNT_RE.fullmatch(subject) is None or object_ not in {"agent", "human"}:
            raise AttributionError("performer_kind_policy requires account subject and kind object")
        if basis != "owner_policy":
            raise AttributionError("performer_kind_policy requires owner_policy basis")
        if not author.startswith("human:"):
            raise AttributionError("owner_policy claims require a human author")
        clean["scope"] = _scope(value.get("scope"), subject)
    elif predicate == "performed_by":
        event_match = _EVENT_RE.fullmatch(subject)
        if event_match is None or not _PRINCIPAL_RE.fullmatch(object_):
            raise AttributionError("performed_by requires event subject and principal object")
        raw_evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else None
        evidence = normalize_receipt({
            key: raw_evidence.get(key) for key in (
                "provider", "account_id", "resource_id", "object_type", "object_id",
            )
        }) if raw_evidence is not None else None
        if evidence is None or canonical_event_id(evidence) != subject:
            raise AttributionError("performed_by evidence must identify its event subject")
        request_id = _text(raw_evidence.get("runtime_request_id"), "runtime_request_id", 160)
        clean["evidence"] = {**evidence, "runtime_request_id": request_id}
        if basis not in {"runtime_receipt", "owner_asserted"}:
            raise AttributionError("performed_by has an invalid basis")
        if basis == "runtime_receipt" and (not author.startswith("agent:") or author != object_):
            raise AttributionError("runtime_receipt author must be the performed-by agent")
        if basis == "owner_asserted" and not author.startswith("human:"):
            raise AttributionError("owner_asserted claims require a human author")
    else:
        if not _CLAIM_RE.fullmatch(subject) or not _CLAIM_RE.fullmatch(object_):
            raise AttributionError("retracts requires claim IDs")
        if basis != "owner_asserted":
            raise AttributionError("retracts requires owner_asserted basis")
        if not author.startswith("human:"):
            raise AttributionError("owner_asserted claims require a human author")
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CLAIM_BYTES:
        raise AttributionError(f"claim exceeds the {MAX_CLAIM_BYTES}-byte limit")
    return clean


class AttributionClaimWriter:
    def __init__(self, shard_path: Path | str):
        self.path = Path(shard_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def append(self, value: Mapping[str, Any]) -> str:
        claim = validate_claim(value)
        encoded = json.dumps(
            claim, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            existing = self.path.read_bytes() if self.path.exists() else b""
            if existing and not existing.endswith(b"\n"):
                raise AttributionStoreError("claim shard has a partial final line")
            for line_number, line in enumerate(existing.splitlines(), 1):
                try:
                    prior = validate_claim(json.loads(line))
                except (json.JSONDecodeError, AttributionError) as exc:
                    raise AttributionStoreError(
                        f"claim shard has an invalid record at line {line_number}",
                    ) from exc
                if prior["id"] != claim["id"]:
                    continue
                if prior == claim:
                    return "duplicate"
                raise AttributionStoreError("claim ID collision with different content")
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.chmod(self.path, 0o600)
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise AttributionStoreError("claim append was incomplete")
                os.fsync(fd)
            finally:
                os.close(fd)
            return "recorded"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


__all__ = [
    "AttributionClaimWriter", "AttributionError", "AttributionStoreError",
    "canonical_account_id", "canonical_event_id", "claim_id_for",
    "is_canonical_agent_id", "normalize_receipt", "performed_by_claim",
    "performer_kind_policy_claim", "retraction_claim", "uses_account_claim",
    "validate_claim",
]
