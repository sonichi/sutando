"""Collect every configured scope before admitting a learning consumer."""
from window_state import plan_windows, record_collection, _timestamp


def collect_pass(directory, scopes, bootstrap_ms, until_ms, enumerate_rooms, collect):
    until_ms = _timestamp(until_ms)
    if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) or not s for s in scopes) or len(set(scopes)) != len(scopes):
        raise ValueError("explicit unique configured scopes required")
    inventory, errors, error_stages = {}, {}, {}
    for scope in scopes:
        stage = "membership_read"
        try:
            rooms = enumerate_rooms(scope)
            stage = "membership_validation"
            if not isinstance(rooms, list) or any(not isinstance(r, str) or not r for r in rooms) or len(set(rooms)) != len(rooms):
                raise ValueError("invalid joined membership")
            inventory[scope] = rooms
        except Exception as exc:
            errors[scope] = type(exc).__name__
            error_stages[scope] = stage
    windows = plan_windows(directory, inventory, bootstrap_ms)
    receipts = {}
    for scope, rooms in inventory.items():
        stage = "window_order"
        try:
            if windows[scope] > until_ms:
                raise ValueError("collection end precedes unread window")
            stage = "history_read"
            value = collect(scope, windows[scope], until_ms)
            stage = "window_validation"
            if not isinstance(value, dict) or value.get("scope") != scope or value.get("since_ms") != windows[scope] or value.get("until_ms") != until_ms:
                raise ValueError("collector window or scope mismatch")
            stage = "membership_validation"
            rows = value.get("rooms")
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) or {r.get("room_id") for r in rows} != set(rooms):
                raise ValueError("collector omitted joined membership")
            stage = "receipt_persistence"
            receipts[scope] = record_collection(directory, scope, value, bootstrap_ms)
        except Exception as exc:
            errors[scope] = type(exc).__name__
            error_stages[scope] = stage
    ready = not errors and len(receipts) == len(scopes) and all(r["complete_available_history"] for r in receipts.values())
    return {"consumer_admitted": ready, "receipts": receipts, "errors": errors, "error_stages": error_stages,
            "learning_progress_advanced": False}
