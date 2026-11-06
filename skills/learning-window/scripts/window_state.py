"""One writer for durable collection receipts and room-scoped collection progress.

Collection progress is not a learned-facts watermark. Callers supply trusted
collector output; this module is not an authorization boundary for arbitrary files.
"""
import fcntl
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid timestamp")
    return value


def _encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _read_state(path):
    state = json.loads(path.read_text()) if path.exists() else {"schema": 1, "scopes": {}}
    if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("scopes"), dict):
        raise ValueError("collection state malformed")
    for current in state["scopes"].values():
        if not isinstance(current, dict) or not isinstance(current.get("rooms"), dict):
            raise ValueError("scope state malformed")
        start = _timestamp(current.get("bootstrap_ms"))
        for row in current["rooms"].values():
            if not isinstance(row, dict) or _timestamp(row.get("collected_through_ms")) < start:
                raise ValueError("room state malformed")
    return state


def plan_windows(directory, memberships, bootstrap_ms):
    bootstrap_ms = _timestamp(bootstrap_ms)
    if not isinstance(memberships, dict):
        raise ValueError("scope inventory required")
    state = _read_state(Path(directory) / "collection-state.json")
    windows = {}
    for scope, rooms in memberships.items():
        if not isinstance(scope, str) or not scope or not isinstance(rooms, list) or any(not isinstance(r, str) or not r for r in rooms):
            raise ValueError("scope room inventory malformed")
        current = state["scopes"].get(scope, {"bootstrap_ms": bootstrap_ms, "rooms": {}})
        if current["bootstrap_ms"] != bootstrap_ms:
            raise ValueError("scope bootstrap changed")
        starts = [current["rooms"].get(r, {}).get("collected_through_ms", bootstrap_ms) for r in rooms]
        windows[scope] = min(starts, default=bootstrap_ms)
    return windows


def _atomic(path, data):
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record_collection(directory, scope, receipt, bootstrap_ms):
    bootstrap_ms = _timestamp(bootstrap_ms)
    if not isinstance(scope, str) or not scope or not isinstance(receipt, dict) or receipt.get("scope") != scope:
        raise ValueError("scope identity required")
    since, until = (_timestamp(receipt.get(k)) for k in ("since_ms", "until_ms"))
    if since > until or since < bootstrap_ms:
        raise ValueError("invalid collection window")
    rows = receipt.get("rooms")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("room inventory required")
    ids = [row.get("room_id") for row in rows]
    if any(not isinstance(r, str) or not r for r in ids) or len(set(ids)) != len(ids) or isinstance(receipt.get("membership_count"), bool) or receipt.get("membership_count") != len(ids):
        raise ValueError("incomplete or duplicate membership inventory")
    data = _encode(receipt)
    digest = hashlib.sha256(data).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundles = directory / "receipts"
    bundles.mkdir(exist_ok=True, mode=0o700)
    lock_fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "collection-state.json"
        state = _read_state(path)
        current = state["scopes"].get(scope, {"bootstrap_ms": bootstrap_ms, "rooms": {}})
        if current.get("bootstrap_ms") != bootstrap_ms or not isinstance(current.get("rooms"), dict):
            raise ValueError("scope bootstrap changed or state malformed")
        for room in ids:
            prior = current["rooms"].get(room, {})
            covered = _timestamp(prior.get("collected_through_ms", bootstrap_ms))
            if since > covered:
                raise ValueError("collection skipped an unread room interval")
        bundle = bundles / (digest + ".json")
        if bundle.exists():
            if bundle.read_bytes() != data:
                raise ValueError("receipt digest collision or corruption")
        else:
            _atomic(bundle, data)
        complete, pending = [], []
        for row in rows:
            room = row["room_id"]
            messages = row.get("messages")
            valid = isinstance(messages, list) and isinstance(row.get("errors"), list) and not row["errors"]
            valid = valid and row.get("coverage") in ("reached_cutoff", "server_no_cursor")
            if valid:
                for event in messages:
                    if not isinstance(event, dict) or not isinstance(event.get("event_id"), str) or not event["event_id"]:
                        valid = False
                        break
                    ts = _timestamp(event.get("ts"))
                    if not since <= ts <= until:
                        valid = False
                        break
            if not valid:
                pending.append(room)
                continue
            prior = current["rooms"].get(room, {})
            if until >= prior.get("collected_through_ms", bootstrap_ms):
                current["rooms"][room] = {"collected_through_ms": until, "receipt_digest": digest}
            complete.append(room)
        state["scopes"][scope] = current
        _atomic(path, _encode(state))
    return {"receipt_digest": digest, "complete_rooms": complete, "pending_rooms": pending,
            "complete_available_history": not pending, "learning_progress_advanced": False}
