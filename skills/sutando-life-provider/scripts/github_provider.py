#!/usr/bin/env python3
"""Bounded read-only GitHub provider edge for Sutando Life."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote


CAPABILITY_ID = "github.activity"
OPERATIONS = ("identity.get", "repositories.list", "repository.events.delta")
MAX_ITEMS = 100
OVERLAP_SECONDS = 300
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]


class ProviderFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool, setup: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.setup = setup


def _default_runner(argv: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout_s, check=False,
    )


def _oauth_setup() -> dict:
    return {
        "kind": "oauth",
        "label": "Authorize GitHub locally",
        "command": ["gh", "auth", "login", "--hostname", "github.com"],
        "restartRequired": True,
    }


def _install_setup() -> dict:
    return {
        "kind": "install",
        "label": "Install GitHub CLI",
        "url": "https://cli.github.com/",
        "restartRequired": True,
    }


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        return MAX_ITEMS
    try:
        return max(1, min(MAX_ITEMS, int(value)))
    except (TypeError, ValueError):
        return MAX_ITEMS


def _text(value: Any, maximum: int = 240) -> Optional[str]:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    return clean[:maximum] if clean else None


def _failure_from_process(proc: subprocess.CompletedProcess) -> ProviderFailure:
    diagnostic = str(proc.stderr or "").casefold()
    if proc.returncode == 4 or any(
        marker in diagnostic for marker in (
            "not logged", "authentication required", "authenticate", "auth login",
        )
    ):
        return ProviderFailure(
            "authorization_required",
            "GitHub authorization is required on this Sutando host.",
            retryable=False,
            setup=_oauth_setup(),
        )
    if "rate limit" in diagnostic:
        return ProviderFailure(
            "rate_limited", "GitHub temporarily rate-limited this read.", retryable=True,
        )
    if any(
        marker in diagnostic
        for marker in ("http 403", "http 404", "forbidden", "not accessible")
    ):
        return ProviderFailure(
            "permission_limited",
            "The authenticated GitHub identity cannot read this resource.",
            retryable=False,
        )
    if any(
        marker in diagnostic
        for marker in ("could not resolve", "network", "timed out", "timeout")
    ):
        return ProviderFailure(
            "provider_unavailable", "GitHub is temporarily unavailable.", retryable=True,
        )
    return ProviderFailure(
        "provider_error", "GitHub could not complete this read.", retryable=True,
    )


def _run_json(runner: _Runner, gh_path: str, args: Sequence[str]) -> Any:
    try:
        proc = runner([gh_path, "api", "--method", "GET", *args], 10.0)
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        raise ProviderFailure(
            "provider_unavailable", "GitHub is temporarily unavailable.", retryable=True,
        ) from exc
    if proc.returncode != 0:
        raise _failure_from_process(proc)
    raw = (
        proc.stdout.decode("utf-8", errors="replace")
        if isinstance(proc.stdout, bytes)
        else proc.stdout
    )
    try:
        return json.loads(raw or "null")
    except (TypeError, ValueError) as exc:
        raise ProviderFailure(
            "invalid_provider_response", "GitHub returned an invalid response.", retryable=True,
        ) from exc


def _error(operation: str, failure: ProviderFailure) -> dict:
    error = {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
    }
    if failure.setup:
        error["setup"] = failure.setup
    return {
        "ok": False,
        "capabilityId": CAPABILITY_ID,
        "operation": operation,
        "partial": False,
        "items": [],
        "error": error,
    }


def _invalid(operation: str, code: str, message: str) -> dict:
    return _error(operation, ProviderFailure(code, message, retryable=False))


def _identity(row: Mapping[str, Any]) -> dict:
    return {
        "id": f"github-user:{row.get('id')}",
        "providerId": str(row.get("id")),
        "login": _text(row.get("login"), 100),
        "name": _text(row.get("name"), 160),
        "avatarUrl": row.get("avatar_url"),
        "evidenceUrl": row.get("html_url"),
    }


def _repository(row: Mapping[str, Any]) -> dict:
    permissions = row.get("permissions") if isinstance(row.get("permissions"), Mapping) else {}
    return {
        "id": f"github-repository:{row.get('id')}",
        "providerId": str(row.get("id")),
        "nameWithOwner": row.get("full_name"),
        "evidenceUrl": row.get("html_url"),
        "private": bool(row.get("private")),
        "archived": bool(row.get("archived")),
        "fork": bool(row.get("fork")),
        "defaultBranch": row.get("default_branch"),
        "updatedAt": row.get("updated_at"),
        "permissions": {
            key: bool(permissions.get(key))
            for key in ("admin", "maintain", "push", "triage", "pull")
            if key in permissions
        },
    }


def _event_evidence(row: Mapping[str, Any], repository: str) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for key in ("pull_request", "issue", "comment", "review", "release", "forkee"):
        value = payload.get(key)
        if isinstance(value, Mapping) and value.get("html_url"):
            return str(value["html_url"])
    head = payload.get("head")
    if isinstance(head, str) and _SHA_RE.fullmatch(head):
        return f"https://github.com/{repository}/commit/{head}"
    return f"https://github.com/{repository}"


def _event_summary(row: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    kind = str(row.get("type") or "Event").removesuffix("Event")
    action = _text(payload.get("action"), 60)
    ref = _text(payload.get("ref"), 160)
    number = None
    for key in ("pull_request", "issue"):
        value = payload.get(key)
        if isinstance(value, Mapping) and value.get("number") is not None:
            number = value.get("number")
            break
    detail = action or ref
    if number is not None:
        detail = f"#{number}" + (f" {action}" if action else "")
    return kind, detail


def _event(row: Mapping[str, Any], repository: str) -> dict:
    actor = row.get("actor") if isinstance(row.get("actor"), Mapping) else {}
    title, detail = _event_summary(row)
    return {
        "id": f"github:{row.get('id')}",
        "providerId": str(row.get("id")),
        "source": "github",
        "repository": repository,
        "kind": f"github.{str(row.get('type') or 'event').removesuffix('Event').lower()}",
        "occurredAt": row.get("created_at"),
        "title": title,
        "detail": detail,
        "actor": {
            "id": f"github-user:{actor.get('id')}",
            "providerId": str(actor.get("id")),
            "login": _text(actor.get("login"), 100),
            "avatarUrl": actor.get("avatar_url"),
            "evidenceUrl": f"https://github.com/{quote(str(actor.get('login') or ''), safe='')}"
            if actor.get("login") else None,
        },
        "evidenceUrl": _event_evidence(row, repository),
    }


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _encode_cursor(timestamp: str, provider_id: str) -> dict:
    return {"version": 1, "ts": timestamp, "id": provider_id}


def _decode_cursor(value: Any) -> Optional[tuple[datetime, str]]:
    if value in (None, ""):
        return None
    if not isinstance(value, Mapping) or value.get("version") != 1:
        raise ValueError("unsupported cursor")
    if not isinstance(value.get("id"), str):
        raise ValueError("invalid cursor payload")
    return _parse_time(value.get("ts")), value["id"]


def _newest_cursor(
    rows: Sequence[Mapping[str, Any]], previous: Optional[tuple[datetime, str]],
) -> Optional[dict]:
    candidates: list[tuple[datetime, str, str]] = []
    for row in rows:
        try:
            created = _parse_time(row.get("created_at"))
        except (TypeError, ValueError):
            continue
        provider_id = str(row.get("id") or "")
        if provider_id:
            candidates.append((created, provider_id, str(row.get("created_at"))))
    if candidates:
        _, provider_id, original = max(candidates, key=lambda item: (item[0], item[1]))
        return _encode_cursor(original, provider_id)
    if previous:
        return _encode_cursor(previous[0].isoformat().replace("+00:00", "Z"), previous[1])
    return None


def _reader(
    runner: _Runner,
    gh_path: str,
    availability: str,
    identity_snapshot: Optional[Mapping[str, Any]],
    startup_failure: Optional[ProviderFailure],
) -> Callable[[dict], dict]:
    def read(params: dict) -> dict:
        operation = str(params.get("operation") or "")
        if operation not in OPERATIONS:
            return _invalid(operation, "unsupported_operation", "Unsupported GitHub operation.")
        if availability == "authorization_required":
            return _error(
                operation,
                ProviderFailure(
                    "authorization_required",
                    "GitHub authorization is required on this Sutando host.",
                    retryable=False,
                    setup=_oauth_setup(),
                ),
            )
        if availability == "unavailable":
            return _error(
                operation,
                startup_failure or ProviderFailure(
                    "provider_unavailable", "GitHub is temporarily unavailable.", retryable=True,
                ),
            )
        try:
            if operation == "identity.get":
                return _read_identity(runner, gh_path, operation, identity_snapshot)
            if operation == "repositories.list":
                return _read_repositories(runner, gh_path, params, operation)
            return _read_events(runner, gh_path, params, operation)
        except ProviderFailure as failure:
            return _error(operation, failure)

    return read


def _read_identity(
    runner: _Runner,
    gh_path: str,
    operation: str,
    identity_snapshot: Optional[Mapping[str, Any]],
) -> dict:
    row = identity_snapshot or _run_json(runner, gh_path, ["user"])
    if not isinstance(row, Mapping) or row.get("id") is None or not row.get("login"):
        raise ProviderFailure(
            "invalid_provider_response", "GitHub returned an invalid identity.", retryable=True,
        )
    return {
        "ok": True,
        "capabilityId": CAPABILITY_ID,
        "operation": operation,
        "partial": False,
        "items": [_identity(row)],
        "limitations": [],
    }


def _read_repositories(runner: _Runner, gh_path: str, params: dict, operation: str) -> dict:
    limit = _bounded_limit(params.get("limit"))
    cursor = params.get("cursor")
    try:
        page = (
            int(cursor.get("page", 1))
            if isinstance(cursor, Mapping)
            else (1 if cursor is None else 0)
        )
    except (TypeError, ValueError):
        return _invalid(
            operation, "invalid_cursor", "Repository cursor must be a positive page number."
        )
    if page < 1 or page > 1000:
        return _invalid(
            operation, "invalid_cursor", "Repository cursor is outside the supported range."
        )
    endpoint = (
        "user/repos?affiliation=owner,collaborator,organization_member"
        f"&sort=updated&per_page={limit}&page={page}"
    )
    rows = _run_json(
        runner,
        gh_path,
        [endpoint],
    )
    if not isinstance(rows, list):
        raise ProviderFailure(
            "invalid_provider_response",
            "GitHub returned an invalid repository list.",
            retryable=True,
        )
    bounded = [
        row
        for row in rows[:limit]
        if isinstance(row, Mapping) and row.get("id") is not None
    ]
    full = len(rows) >= limit
    return {
        "ok": True,
        "capabilityId": CAPABILITY_ID,
        "operation": operation,
        "partial": False,
        "items": [_repository(row) for row in bounded],
        "nextCursor": {"page": page + 1} if full else None,
        "limitations": [],
    }


def _read_events(runner: _Runner, gh_path: str, params: dict, operation: str) -> dict:
    resource = params.get("resource")
    repository = resource.get("repository") if isinstance(resource, Mapping) else None
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        return _invalid(operation, "invalid_resource", "resource.repository must be owner/name.")
    owner, name = repository.split("/", 1)
    if owner in (".", "..") or name in (".", ".."):
        return _invalid(operation, "invalid_resource", "resource.repository must be owner/name.")
    try:
        previous = _decode_cursor(params.get("cursor"))
    except (ValueError, TypeError):
        return _invalid(operation, "invalid_cursor", "Event cursor is invalid or unsupported.")
    limit = _bounded_limit(params.get("limit"))
    endpoint = (
        f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        f"/events?per_page={limit}&page=1"
    )
    rows = _run_json(runner, gh_path, [endpoint])
    if not isinstance(rows, list):
        raise ProviderFailure(
            "invalid_provider_response", "GitHub returned an invalid event list.", retryable=True,
        )
    valid = [row for row in rows[:limit] if isinstance(row, Mapping) and row.get("id") is not None]
    if previous:
        floor = previous[0] - timedelta(seconds=OVERLAP_SECONDS)
        valid = [row for row in valid if _event_at_or_after(row, floor)]
    full = len(rows) >= limit
    limitations = []
    if full:
        limitations.append({
            "code": "response_limit_reached",
            "message": "Older repository activity may exist beyond this bounded read.",
        })
    return {
        "ok": True,
        "capabilityId": CAPABILITY_ID,
        "operation": operation,
        "partial": full,
        "items": [_event(row, repository) for row in valid],
        "nextCursor": _newest_cursor(valid, previous),
        "coverage": {
            "overlapSeconds": OVERLAP_SECONDS,
            "gapPossible": full,
            "received": len(rows),
            "returned": len(valid),
        },
        "limitations": limitations,
    }


def _event_at_or_after(row: Mapping[str, Any], floor: datetime) -> bool:
    try:
        return _parse_time(row.get("created_at")) >= floor
    except (TypeError, ValueError):
        return False


def registry_inputs(
    *, run_gh: Optional[_Runner] = None, gh_path: Optional[str] = None,
) -> tuple[dict[str, dict], dict[str, Callable[[dict], dict]]]:
    runner = run_gh or _default_runner
    resolved_gh = shutil.which("gh") if gh_path is None else gh_path
    identity_snapshot: Optional[Mapping[str, Any]] = None
    startup_failure: Optional[ProviderFailure] = None
    if not resolved_gh:
        availability = "unavailable"
        setup = _install_setup()
        startup_failure = ProviderFailure(
            "provider_unavailable",
            "GitHub CLI is unavailable on this Sutando host.",
            retryable=False,
            setup=setup,
        )
    else:
        try:
            probe = _run_json(runner, resolved_gh, ["user"])
            if not isinstance(probe, Mapping) or probe.get("id") is None or not probe.get("login"):
                raise ProviderFailure(
                    "invalid_provider_response",
                    "GitHub returned an invalid identity.",
                    retryable=True,
                )
            identity_snapshot = {
                key: probe.get(key)
                for key in ("id", "login", "name", "avatar_url", "html_url")
            }
            availability = "ready"
            setup = None
        except ProviderFailure as failure:
            availability = "authorization_required"
            if failure.code != "authorization_required":
                availability = "unavailable"
            setup = _oauth_setup() if availability == "authorization_required" else None
            startup_failure = failure
        except (OSError, subprocess.TimeoutExpired, TimeoutError):
            availability = "unavailable"
            setup = None
            startup_failure = ProviderFailure(
                "provider_unavailable", "GitHub is temporarily unavailable.", retryable=True,
            )
    descriptor = {
        "id": CAPABILITY_ID,
        "version": 1,
        "availability": availability,
        "description": (
            "Read the local Sutando user's GitHub identity, repositories, "
            "and recent repository events."
        ),
        "operations": list(OPERATIONS),
        "constraints": {
            "readOnly": True,
            "maxItems": MAX_ITEMS,
            "eventOverlapSeconds": OVERLAP_SECONDS,
            "availabilitySnapshot": "runtime-start",
        },
        "setup": setup or {},
        "metadata": {"provider": "github", "credentialOwner": "sutando"},
    }
    if identity_snapshot is not None:
        descriptor["identity"] = {
            "id": f"github-user:{identity_snapshot.get('id')}",
            "login": _text(identity_snapshot.get("login"), 100),
        }
    return (
        {CAPABILITY_ID: descriptor},
        {CAPABILITY_ID: _reader(
            runner, resolved_gh or "gh", availability, identity_snapshot, startup_failure,
        )},
    )


__all__ = ["CAPABILITY_ID", "MAX_ITEMS", "OPERATIONS", "OVERLAP_SECONDS", "registry_inputs"]
