"""Bounded window pagination with explicit population and coverage receipts."""
import math


def collect_window(room_ids, fetch, since_ms, until_ms, pages=20):
    if since_ms > until_ms or not 1 <= pages <= 100:
        raise ValueError("invalid window or page budget")
    if not isinstance(room_ids, list) or any(not isinstance(r, str) or not r for r in room_ids):
        raise ValueError("malformed membership inventory")
    rooms = []
    for room_id in dict.fromkeys(room_ids):
        row = {"room_id": room_id, "messages": [], "pages": 0, "coverage": "unknown",
               "errors": [], "cursors": [], "oldest_ts": None, "newest_ts": None}
        seen, cursors, before, timestamps = set(), set(), None, []
        try:
            for _ in range(pages):
                page = fetch(room_id, before)
                row["pages"] += 1
                if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
                    row["errors"].append("malformed page")
                    break
                if page.get("ok") is False or page.get("error"):
                    row["errors"].append("page declined")
                    break
                times = []
                for message in page["messages"]:
                    if not isinstance(message, dict) or not isinstance(message.get("ts"), (int, float)) or isinstance(message.get("ts"), bool) or not math.isfinite(message["ts"]) or not isinstance(message.get("event_id"), str) or not message["event_id"]:
                        row["errors"].append("malformed event")
                        continue
                    ts, key = message["ts"], message["event_id"]
                    times.append(ts)
                    timestamps.append(ts)
                    if since_ms <= ts <= until_ms and key not in seen:
                        row["messages"].append(message)
                        seen.add(key)
                if times and min(times) < since_ms:
                    row["coverage"] = "reached_cutoff"
                    break
                cursor = page.get("cursor")
                row["cursors"].append(cursor)
                if cursor is None or cursor == "":
                    row["coverage"] = "server_no_cursor"
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    row["coverage"] = "cursor_stalled"
                    break
                cursors.add(cursor)
                before = cursor
            else:
                row["coverage"] = "page_budget_exhausted"
        except Exception as exc:
            row["coverage"] = "read_failed"
            row["errors"].append(type(exc).__name__)
        row["oldest_ts"] = min(timestamps) if timestamps else None
        row["newest_ts"] = max(timestamps) if timestamps else None
        row["window_messages"] = len(row["messages"])
        row["covered_available_history"] = not row["errors"] and row["coverage"] in ("reached_cutoff", "server_no_cursor")
        rooms.append(row)
    return {"ok": all(r["covered_available_history"] for r in rooms), "rooms": rooms,
            "membership_count": len(rooms), "window_messages": sum(r["window_messages"] for r in rooms),
            "complete_available_history": all(r["covered_available_history"] for r in rooms),
            "since_ms": since_ms, "until_ms": until_ms,
            "note": "Server history boundaries do not prove retention of deleted or unavailable events."}
