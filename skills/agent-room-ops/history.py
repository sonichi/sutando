"""Read all currently joined rooms through an explicit window, without cursor writes."""
import json
from urllib.parse import urlsplit

from _gateway import gateway, http_request, quote, urlencode, load_gate, gate_allows
from rooms import joined_rooms
from read import _redactor
from history_collection import collect_window


def history(since_ms, until_ms, pages=20, agent_mxid=None):
    inventory = joined_rooms(agent_mxid)
    if not inventory.get("ok"):
        return {"ok": False, "complete_available_history": False, "reason": "Membership enumeration unavailable", "rooms": []}
    base, headers = gateway()
    if not base:
        return {"ok": False, "complete_available_history": False, "reason": "Gateway unavailable", "rooms": []}
    gate = load_gate()
    redact = _redactor()

    def fetch(room_id, before):
        if not gate_allows(agent_mxid, room_id, gate):
            raise PermissionError("client gate")
        params = {"limit": 100}
        if before:
            params["before"] = before
        _, body, _ = http_request("GET", base + "/v1/rooms/" + quote(room_id) + "/messages?" + urlencode(params), headers)
        page = json.loads(body)
        if isinstance(page, dict) and isinstance(page.get("messages"), list):
            for message in page["messages"]:
                if isinstance(message, dict):
                    for field in ("body", "text", "message", "formatted_body"):
                        if isinstance(message.get(field), str):
                            message[field] = redact(message[field])
        return page

    result = collect_window(inventory.get("rooms"), fetch, since_ms, until_ms, pages)
    parsed = urlsplit(base)
    result["scope"] = parsed.hostname
    return result
