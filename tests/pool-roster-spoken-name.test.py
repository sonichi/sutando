#!/usr/bin/env python3
"""A worker named by voice resolves by the pool's own policy, normalised.

"Pro-dev", "pro dev" and "PRO_DEV" name the worker labelled pro-dev; an
unknown name is refused (not sent to the core), a shared one is ambiguous,
and the resolved id is what the router then delivers to.

Run: python3 tests/pool-roster-spoken-name.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import pool_roster as pr  # noqa: E402
import pool_router as rt  # noqa: E402

FAILURES: list[str] = []
ROOM = "!room:ag2.space"
ROSTER = {
    "version": 3,
    "workers": {
        "w-dev": {"label": "pro-dev", "state": "live"},
        "w-comm": {"label": "pro-comm", "state": "live"},
        "w-fast": {"label": "pro-fast", "display_label": "Pro Fast", "state": "live"},
        "w-a": {"label": "a", "display_label": "Twin", "state": "live"},
        "w-b": {"label": "b", "display_label": "Twin", "state": "live"},
    },
    "bindings": {ROOM: "w-comm"},
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    for said, want in (("Pro-dev", "pro-dev"), ("pro dev", "pro-dev"), (" PRO__DEV ", "pro-dev"), ("Pro - Fast", "pro-fast")):
        check(f"normalise {said!r}", pr.normalize_spoken_name(said) == want, pr.normalize_spoken_name(said))
    for said in ("Pro-dev", "pro dev", "PRO_DEV", "w-dev"):
        got = pr.resolve_spoken_name(ROSTER, said)
        check(f"{said!r} -> w-dev", got == {"worker": "w-dev"}, str(got))
    got = pr.resolve_spoken_name(ROSTER, "pro fast")
    check("display label matches normalised", got == {"worker": "w-fast"}, str(got))
    check("core is a recipient", pr.resolve_spoken_name(ROSTER, "Core") == {"worker": "core"})
    got = pr.resolve_spoken_name(ROSTER, "pro-design")
    check("unknown is refused with the names", got.get("error") == "unknown"
          and got.get("known") == ["Pro Fast", "Twin", "pro-comm", "pro-dev"], str(got))
    check("shared name is ambiguous", pr.resolve_spoken_name(ROSTER, "twin").get("error") == "ambiguous")
    check("empty is unknown", pr.resolve_spoken_name(ROSTER, "  - ").get("error") == "unknown")

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        for d in ("tasks", "state", "deliveries"):
            (ws / d).mkdir()
        (ws / "state" / "roster.json").write_text(json.dumps(ROSTER))
        out = subprocess.run([sys.executable, str(SCRIPTS / "resolve_worker_name.py"), "--workspace", str(ws), "--", "Pro dev"],
                             capture_output=True, text=True)
        check("script prints the verdict", out.returncode == 0 and json.loads(out.stdout) == {"worker": "w-dev"}, out.stdout + out.stderr)
        out = subprocess.run([sys.executable, str(SCRIPTS / "resolve_worker_name.py"), "--workspace", str(ws / "none"), "--", "-x"],
                             capture_output=True, text=True)
        check("no roster, dash-led name", out.returncode == 0 and json.loads(out.stdout)["error"] == "no-roster", out.stdout + out.stderr)
        worker = pr.resolve_spoken_name(ROSTER, "pro dev")["worker"]
        res = rt.route(ws, {"id": "task-v1", "source": "voice", "channel_id": ROOM, "requested_worker": worker}, ROSTER)
        check("router delivers the resolved id, over the room binding", res["targets"] == ["w-dev"], str(res))
        res = rt.route(ws, {"id": "task-v2", "source": "voice", "channel_id": ROOM}, ROSTER)
        check("room-docked default goes to the bound worker", res["targets"] == ["w-comm"], str(res))
        res = rt.route(ws, {"id": "task-v3", "source": "voice", "channel_id": "local-voice"}, ROSTER)
        check("no room goes to the core", res["targets"] == ["core"], str(res))

    print("all passed" if not FAILURES else f"{len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
