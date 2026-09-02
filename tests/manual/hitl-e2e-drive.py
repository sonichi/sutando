"""Full-lifecycle HITL E2E harness (owner spec steps 1-9), safe on a live core.

Forces AUTH_REQUIRED with a FAKE logged-out probe runner — the live core is
never logged out — while the SENDER is the real gateway, so a real card lands
in a real room. The action leg exercises the wire contract exactly (a stale
reply must be rejected, a fresh one accepted), and the resolution leg uses the
REAL `claude auth status` probe: reality is logged-in, so the requirement
resolves and the card EDITs to resolved.

Usage:
  set -a; . "$(bash scripts/channel-env.sh ag2space)"; set +a
  python3 tests/hitl-e2e-drive.py --room '!room:ag2.space'
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.detector import drive  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.projector import project  # noqa: E402
from hitl.schema import ActionReply, StaleRequirementError  # noqa: E402

URL = os.environ.get("REMOTE_TASK_URL", "").rstrip("/")
TOKEN = os.environ.get("REMOTE_TASK_TOKEN", "")


def gateway_send(payload):
    payload = dict(payload)
    payload.setdefault("op", "message")
    req = urllib.request.Request(f"{URL}/v1/room", data=json.dumps(payload).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "sutando-gateway-client/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


LOGGED_OUT = json.dumps({"loggedIn": False, "authMethod": "claude.ai"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    args = ap.parse_args()
    assert URL and TOKEN, "load the ag2space channel env first"

    tmp = tempfile.mkdtemp(prefix="hitl-e2e-")
    mgr = HitlManager(HitlStore(Path(tmp)))
    device = {"id": "qingyun-air", "name": "Qingyun-Airs-MacBook-Air"}

    print("== 1. force AUTH_REQUIRED (fake logged-out probe; live auth untouched)")
    out = drive(mgr, device=device, runner=lambda cmd: (0, LOGGED_OUT))
    req = mgr.get(out.created)
    print(f"   requirement {req.id} kind={req.kind} status={req.status} rev={req.revision} guard={req.guard}")

    print("== 2. link blocked work")
    mgr.link_blocked_task(req.id, "task-e2e-demo")

    print("== 3. project: CREATE card in room (real gateway send)")
    done = project(mgr, gateway_send, args.room)
    assert done and done[0][1], f"CREATE not accepted: {done}"
    event_id = done[0][1]
    print(f"   card event {event_id}")

    print("== 4. stale action MUST be rejected (wrong revision)")
    stale = ActionReply(hitl_id=req.id, expected_revision=99, action_id="reauth", guard=req.guard)
    try:
        mgr.apply_action(stale)
        raise AssertionError("stale action was accepted — gate failed")
    except StaleRequirementError as e:
        print(f"   rejected as required: {e}")

    print("== 5. fresh action accepted -> in_progress")
    req = mgr.get(req.id)
    action = mgr.apply_action(
        ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id="reauth", guard=req.guard)
    )
    print(f"   action kind={action.kind}; status={mgr.get(req.id).status} rev={mgr.get(req.id).revision}")

    print("== 6. project: EDIT card to in_progress")
    project(mgr, gateway_send, args.room)

    print("== 7. resolution probe: REAL `claude auth status` (reality: logged in)")
    out = drive(mgr, device=device)  # default runner = real CLI
    assert out.resolved == [req.id], f"expected resolve, got {out}"
    print(f"   resolved {out.resolved}, resumed blocked work: {out.resumed_tasks}")
    assert out.resumed_tasks == ["task-e2e-demo"]

    print("== 8. project: EDIT card to resolved")
    project(mgr, gateway_send, args.room)
    final = mgr.get(req.id)
    print(f"   final status={final.status} rev={final.revision}; "
          f"projection rev={mgr.store.projection(req.id)['revision']} target={mgr.projection_target(req.id)}")
    assert final.status == "resolved" and not mgr.needs_projection(req.id)

    print(f"== 9. DONE — verify in-room: card {event_id} should read resolved (edited twice)")
    print(json.dumps({"event_id": event_id, "requirement": final.to_wire(), "store": tmp}))


if __name__ == "__main__":
    main()
