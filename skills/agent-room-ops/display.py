#!/usr/bin/env python3
"""Set the agent-owned display config a room's clients render worker UI from.

Writes the 'space.ag2.display' room state event. User localStorage overrides
still win client-side; this is the agent's declared layer above the defaults.

Env: MATRIX_HS_URL (e.g. http://localhost:8080), MATRIX_AS_TOKEN (appservice
token), AGENT_MXID (user to act as). Usage:
  display.py <room_id> [--stripe on|off] [--base-color '#rrggbb']
             [--corner tl|tr|bl|br] [--shape corner|star]
             [--worker-color <id>=<#rrggbb> ...] [--clear]
Merges onto the existing event unless --clear is given.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _req(url: str, token: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("room_id")
    ap.add_argument("--stripe", choices=["on", "off"])
    ap.add_argument("--base-color")
    ap.add_argument("--corner", choices=["tl", "tr", "bl", "br"])
    ap.add_argument("--shape", choices=["corner", "star"])
    ap.add_argument("--worker-color", action="append", default=[])
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args(argv)

    hs = os.environ.get("MATRIX_HS_URL", "").rstrip("/")
    token = os.environ.get("MATRIX_AS_TOKEN", "")
    mxid = os.environ.get("AGENT_MXID", "")
    if not (hs and token and mxid):
        print("display.py: MATRIX_HS_URL, MATRIX_AS_TOKEN and AGENT_MXID must be set",
              file=sys.stderr)
        return 2

    room = urllib.parse.quote(a.room_id, safe="")
    uid = urllib.parse.quote(mxid, safe="")
    base = f"{hs}/_matrix/client/v3/rooms/{room}/state/space.ag2.display/"

    content = {}
    if not a.clear:
        st, cur = _req(f"{base}?user_id={uid}", token)
        if st == 200 and isinstance(cur, dict):
            content = cur
    if a.stripe:
        content["stripe"] = a.stripe == "on"
    if a.base_color:
        if not HEX.match(a.base_color):
            print("display.py: --base-color must be #rrggbb", file=sys.stderr)
            return 2
        content["baseColor"] = a.base_color
    if a.corner:
        content["corner"] = a.corner
    if a.shape:
        content["shape"] = a.shape
    for wc in a.worker_color:
        wid, _, col = wc.partition("=")
        if not (wid and HEX.match(col)):
            print(f"display.py: bad --worker-color {wc!r}", file=sys.stderr)
            return 2
        content.setdefault("colors", {})[wid] = col

    st, out = _req(f"{base}?user_id={uid}", token, "PUT", content)
    print(json.dumps({"ok": st == 200, "status": st, "content": content, "resp": out}))
    return 0 if st == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
