#!/usr/bin/env python3
"""With status-ack off, nothing of it runs on the live path.

  a) notify sends a progress notice and writes no `progress:<task>:sent` claim
     CONTROL: switched on, the same send writes it
  b) a fault while recording the claim never fails a notify that was sent (rc 0)
  c) with ack_claim and ack_overlap unimportable, a proactive message about a task is still
     delivered through the gateway's real drain and the host's dispatch and route gates
  (The bridge installing no ack gate while off is pinned in src/remote-gateway-bridge.test.py.)

Run: python3 tests/status-ack-off-hooks.test.py
"""
from __future__ import annotations

import functools
import importlib
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402

ISO = pl.scratch("status-ack-off-hooks-")
check = pl.Checks()
TID = "task-1761300000001"


def load_notify():
    spec = importlib.util.spec_from_file_location("notify_off_hooks", sa.REPO / "skills" / "task-progress" / "scripts" / "notify.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


notify = load_notify()
POSTS: list = []
notify._post = lambda url, payload, headers, timeout=10: POSTS.append(dict(payload)) or True


def send(tf: Path, msg: str) -> int:
    saved = sys.argv
    sys.argv = ["notify.py", "--task-file", str(tf), "--message", msg]
    try:
        return notify.main()
    finally:
        sys.argv = saved


def main() -> int:
    os.environ.pop("STATUS_ACK_ENABLED", None)
    lane = pl.Lane(gb, ISO)
    tf = sa.task(lane.ws, TID)
    rc = (send(tf, "On it."), send(tf, "Still on it."))
    check(rc == (0, 0) and len(POSTS) == 2 and ack_claim.state(lane.ws, f"progress:{TID}:sent") == "none",
          f"a) off: notices sent, no sent claim written ({rc}, {len(POSTS)})")

    os.environ["STATUS_ACK_ENABLED"] = "1"
    try:
        lane = pl.Lane(gb, ISO)
        tf = sa.task(lane.ws, TID)
        send(tf, "On it.")
        check(ack_claim.state(lane.ws, f"progress:{TID}:sent") == "claimed", "CONTROL (a): on, the send records it")

        lane = pl.Lane(gb, ISO)
        tf = sa.task(lane.ws, TID)
        real = notify._ACK_ARBITRATION
        def claim(ws, key, *a, **k):
            if key.endswith(":sent"):
                raise TypeError("bad record")
            return real[0].claim(ws, key, *a, **k)
        broken = type("Broken", (), {"claim": staticmethod(claim), "update": staticmethod(real[0].update)})
        notify._ACK_ARBITRATION = (broken, real[1], real[2])
        del POSTS[:]
        try:
            rc = send(tf, "Still on it.")
        except Exception as exc:  # noqa: BLE001 - the old head lets it escape; that is the failure
            rc = repr(exc)
        finally:
            notify._ACK_ARBITRATION = real
        check(rc == 0 and len(POSTS) == 1, f"b) a fault recording the claim leaves a sent notify at rc 0 ({rc})")
    finally:
        os.environ.pop("STATUS_ACK_ENABLED", None)

    saved = {m: sys.modules.get(m) for m in ("ack_claim", "ack_overlap", "proactive_dispatch_gate")}
    sys.modules["ack_claim"] = sys.modules["ack_overlap"] = None
    sys.modules.pop("proactive_dispatch_gate", None)
    try:
        gate = importlib.import_module("proactive_dispatch_gate")
        lane = pl.Lane(gb, ISO)
        sa.task(lane.ws, TID)
        lane.write(f"proactive-about-{TID}--1761300000500.to-ag2space.txt",
                   f"[channel: {sa.ROOM}]\n[thread: {sa.MSG_EVT}]\nAn update about your request.\n")
        lane.drain(gate=functools.partial(gate.dispatch_veto, workspace=lane.ws),
                   route_gate=functools.partial(gate.route_veto, workspace=lane.ws))
        ok = [p.get("body") for p in sa.room_posts(lane)] == ["An update about your request."]
        detail = (lane.posts, lane.logs[-1:])
    except Exception as exc:  # noqa: BLE001 - the old head's import fault is what this case pins
        ok, detail = False, repr(exc)
    finally:
        for m, v in saved.items():
            if v is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = v
    check(ok, f"c) ack modules unimportable: a proactive message about a task is still delivered ({detail})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
