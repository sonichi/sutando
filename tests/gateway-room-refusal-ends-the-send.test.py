#!/usr/bin/env python3
"""A room that refuses the sender ends the send; it is not retried as a credential failure.

The gateway answers a room message for a room the agent is not in with a 403 whose JSON error
says so. The bearer was accepted, so neither a retry nor a token refresh changes the answer.
The room-message provider used to read every 403 as a recoverable auth failure: the message
was sent again on the outbox's schedule until its retry window closed, and each attempt was
raised to the worker as an auth rejection.

Now that 403 gets a small bounded budget (`ROOM_REFUSAL_ATTEMPTS` sends on the outbox's own
backoff, so a join still landing delivers), then the item parks as `permanent-refusal`, the file
moves to undelivered/ with a log line, and nothing reaches the auth path. A 403 that names no
room refusal (an edge page, a bare status) keeps today's handling, and stays bounded by the
retry window.

Run: python3 tests/gateway-room-refusal-ends-the-send.test.py
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from _helpers.hermetic_gateway import isolate_then_import

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})

from _helpers import proactive_lane as pl
from ag2_sparrow.delivery_core import ProviderBoundedRefused, ProviderPermanentRefused, ProviderRefused
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceRoomMessageProvider
import importlib.util
import os
import shlex
import subprocess


from ag2_sparrow import send_failure_policy
from ag2_sparrow.delivery_core.provider_ag2space import room_refused_sender
from ag2_sparrow.send_failure_policy import ROOM_REFUSAL_ATTEMPTS

# By file path: the src copy is canonical, the package one is vendored from it.
_SPEC = importlib.util.spec_from_file_location(
    "src_send_failure_policy", Path(__file__).resolve().parents[1] / "src" / "send_failure_policy.py")
src_send_failure_policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(src_send_failure_policy)

ISO = pl.scratch("gw-room-refusal-")
check = pl.Checks()

NOT_IN_ROOM = {"error": "send failed: sender is not in this room (HTTP 403)"}
REPO = Path(__file__).resolve().parents[1]
# What the Sutando adapter injects: logged commands name the workspace through it.
RESOLVER = "bash scripts/sutando-config.sh workspace"
# The outbox's retry window is 600s; an hour of 1s scans is well past it.
SCANS = 3600


def refuse(code: int, body: "dict | None" = None):
    """A relay that answers every room message with `code`, and `body` as its JSON error."""
    def respond(payload):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        raise urllib.error.HTTPError("https://gw.example/relay/v1/room", code, "err", {}, io.BytesIO(raw))
    return respond


def refuse_until_joined(refusals: int):
    """A room that refuses the sender's first `refusals` sends, then accepts: a join landing."""
    seen = {"n": 0}

    def respond(payload):
        seen["n"] += 1
        if seen["n"] <= refusals:
            refuse(403, NOT_IN_ROOM)(payload)
        return {"ok": True, "event_id": f"$joined{seen['n']}"}
    return respond


def scan_for_an_hour(lane: pl.Lane, stem: str) -> "tuple[int, list]":
    """One drain a second for an hour of outbox time; (auth errors raised to the worker, parks)."""
    backend = lane.backend()
    clock = {"now": 1_800_000_000.0}
    saved, backend.clock = backend.clock, lambda: clock["now"]
    raised = 0
    try:
        for _ in range(SCANS):
            if lane.where(stem) != "pending":
                break
            try:
                lane.drain()
            except urllib.error.HTTPError:
                raised += 1
            clock["now"] += 1.0
    finally:
        backend.clock = saved
    return raised, [i for i in lane.items() if i.get("status") == "PARKED"]


def classifier() -> None:
    for where, module in (("src", src_send_failure_policy), ("package", send_failure_policy)):
        fn = getattr(module, "is_room_membership_refusal", None)
        check(callable(fn), f"the {where} send-failure policy names a room refusal")
        if callable(fn):
            classify(fn, where)


def classify(is_room_membership_refusal, where: str) -> None:
    for body in (NOT_IN_ROOM, {"error": "not a room member (HTTP 403)"},
                 {"error": "Sender is not in this room"}, {"error": "User @a:s NOT IN ROOM !r:s"},
                 {"message": "sender is not in this room"}, {"detail": "not a room member"},
                 {"message": "Send Failed: Sender Is Not In This Room"}, {"detail": "NOT A ROOM MEMBER"},
                 {"errcode": "M_FORBIDDEN", "error": "User @a:s not in room !r:s"},
                 json.dumps(NOT_IN_ROOM), json.dumps(NOT_IN_ROOM).encode("utf-8")):
        check(is_room_membership_refusal(403, body), f"{where}: a room refusal is recognised: {str(body)[:50]}")
    for status, body in ((403, b""), (403, "<html>error 1010</html>"), (403, {"error": "unauthorized"}),
                         (403, {"error": "forbidden"}), (403, None), (403, ["not in this room"]),
                         (403, {"errcode": "M_FORBIDDEN", "error": "Invalid access token"}),
                         (403, {"error": "not a member of this workspace"}),
                         (403, {"error": "user is not a member of the organization"}),
                         (401, NOT_IN_ROOM), (429, NOT_IN_ROOM), (500, NOT_IN_ROOM)):
        check(not is_room_membership_refusal(status, body),
              f"{where}: not a room refusal: HTTP {status} {str(body)[:40]!r}")


def an_unreadable_body_is_no_room_refusal() -> None:
    class Unreadable(urllib.error.HTTPError):
        def read(self, *a):
            raise OSError("connection reset")
    e = Unreadable("https://gw.example/relay/v1/room", 403, "err", {}, io.BytesIO(b""))
    check(room_refused_sender(e) is False, "a 403 whose body cannot be read is not a room refusal")
    check(e.sparrow_body.getvalue() == b"", "and later readers see the same empty body, not a second read")


def provider() -> None:
    def deliver(respond):
        sent = []

        def send(room, body, key, *thread):
            sent.append(room)
            return respond({"room_id": room})
        prov = AG2SpaceRoomMessageProvider(send, lambda *a: None)
        try:
            prov.deliver("i1", b'{"room_id": "!gone:s", "body": "b"}', "k1")
        except ProviderRefused as e:
            return e
        return None

    got = deliver(refuse(403, NOT_IN_ROOM))
    check(isinstance(got, ProviderBoundedRefused) and not isinstance(got, ProviderPermanentRefused)
          and got.attempts == ROOM_REFUSAL_ATTEMPTS and "!gone:s" in str(got),
          f"the provider gives a room refusal a bounded budget, naming the room ({type(got).__name__}: {got})")
    got = deliver(refuse(403))
    check(isinstance(got, ProviderRefused) and not isinstance(got, ProviderPermanentRefused),
          f"a 403 that names no room refusal is still refused for now ({type(got).__name__})")
    got = deliver(refuse(401, NOT_IN_ROOM))
    check(isinstance(got, ProviderRefused) and not isinstance(got, ProviderPermanentRefused),
          f"a 401 is the bearer's whatever its body says ({type(got).__name__})")


def a_room_refusal_parks_after_its_budget() -> None:
    lane = pl.Lane(gb, ISO, refuse(403, NOT_IN_ROOM))
    lane.write("proactive-notinroom.txt", "[channel: !gone:example.org]\nhourly report\n")
    saved, gb._WORKSPACE_RESOLVER = getattr(gb, "_WORKSPACE_RESOLVER", None), RESOLVER
    try:
        raised, parked = scan_for_an_hour(lane, "proactive-notinroom")
    finally:
        gb._WORKSPACE_RESOLVER = saved
    check(2 <= ROOM_REFUSAL_ATTEMPTS <= 5, f"the budget is small ({ROOM_REFUSAL_ATTEMPTS})")
    check(len(lane.posts) == ROOM_REFUSAL_ATTEMPTS,
          f"a room refusal is sent exactly {ROOM_REFUSAL_ATTEMPTS} times ({len(lane.posts)} sends in an hour of scans)")
    check(raised == 0, f"a room refusal never reaches the worker as an auth rejection ({raised} raised)")
    check(lane.where("proactive-notinroom") == "undelivered",
          f"the refused message is moved to undelivered/ ({lane.where('proactive-notinroom')})")
    check(len(parked) == 1 and parked[0].get("reason") == "permanent-refusal",
          f"its outbox item parks as a permanent refusal ({[p.get('reason') for p in parked]})")
    check(any("PARKED" in m and "permanent-refusal" in m and "will NOT be re-sent" in m for m in lane.logs),
          "the park is logged with its reason")
    line = next((m.split("To send it again: ", 1)[1] for m in lane.logs
                 if "permanent-refusal" in m and "To send it again: " in m), None)
    cmd = shlex.split(line) if line else None
    check(cmd is not None and "--reset-attempts" in cmd,
          f"and with the requeue command, restoring the full budget ({cmd})")
    check(line is not None and str(lane.ws) not in line and str(lane.ws.resolve()) not in line
          and f'--root "$({RESOLVER})"/results/' in line,
          f"the command names its root through the workspace resolver, not a path ({line})")
    if cmd:
        # As an operator pastes it: a shell in the checkout, the resolver printing this workspace.
        ran = subprocess.run(["bash", "-c", line], cwd=REPO, capture_output=True, text=True, timeout=60,
                             env={**os.environ, "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(lane.ws)})
        check(ran.returncode == 0, f"the logged command requeues it ({ran.returncode}: {ran.stderr[-300:]})")
        lane.respond = refuse_until_joined(ROOM_REFUSAL_ATTEMPTS - 1)
        before = len(lane.posts)
        scan_for_an_hour(lane, "proactive-notinroom")
        check(len(lane.posts) - before == ROOM_REFUSAL_ATTEMPTS
              and lane.where("proactive-notinroom") == "archived",
              f"and the requeued message gets the whole budget again "
              f"({len(lane.posts) - before} sends, {lane.where('proactive-notinroom')})")


def a_logged_command_names_no_home_path() -> None:
    home = Path.home()
    under_home = home / "Library" / "Application Support" / "ws" / "results" / ".outbox-proactive"
    saved, gb._WORKSPACE_RESOLVER = getattr(gb, "_WORKSPACE_RESOLVER", None), ""
    try:
        bare = gb._requeue_command(under_home, "wr_1.c")
    finally:
        gb._WORKSPACE_RESOLVER = saved
    check(str(home) not in bare and bare.startswith('python3 src/outbox_cli.py --root "$HOME"/'),
          f"without a resolver, a root under the home dir is named from $HOME ({bare})")
    ran = subprocess.run(["bash", "-c", f"printf '%s\\n' {bare.split(' --root ', 1)[1]}"],
                         capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
    check(ran.stdout.splitlines() == [str(under_home), "requeue", "wr_1.c"],
          f"and the shell expands it back to the root, spaces intact ({ran.stdout!r})")
    elsewhere = Path("/srv/agent ws/results/.outbox-proactive")
    got = shlex.split(gb._requeue_command(elsewhere, "wr_1.c"))
    check(got[3] == str(elsewhere), f"a root outside both is quoted whole ({got})")


def a_join_landing_on_the_third_send_delivers() -> None:
    lane = pl.Lane(gb, ISO, refuse_until_joined(2))
    lane.write("proactive-joinrace.txt", "[channel: !joining:example.org]\ngreeting\n")
    raised, parked = scan_for_an_hour(lane, "proactive-joinrace")
    check(len(lane.posts) == 3, f"refused twice, the third send lands ({len(lane.posts)} sends)")
    check(raised == 0 and parked == [], f"nothing reaches the auth path or parks ({raised}, {parked})")
    check(lane.where("proactive-joinrace") == "archived",
          f"the message is delivered and archived ({lane.where('proactive-joinrace')})")


def another_403_keeps_its_handling_and_stays_bounded() -> None:
    lane = pl.Lane(gb, ISO, refuse(403))
    lane.write("proactive-bare403.txt", "nudge\n")
    raised, parked = scan_for_an_hour(lane, "proactive-bare403")
    check(raised == len(lane.posts) >= 1,
          f"a 403 that names no room refusal still reaches the auth path on each send ({raised}/{len(lane.posts)})")
    check(5 <= len(lane.posts) <= 40,
          f"and is bounded by the retry window ({len(lane.posts)} sends in an hour of scans)")
    check(lane.where("proactive-bare403") == "undelivered"
          and [p.get("reason") for p in parked] == ["retry-window-exhausted"],
          f"then parks ({lane.where('proactive-bare403')}, {[p.get('reason') for p in parked]})")


def main() -> int:
    classifier()
    an_unreadable_body_is_no_room_refusal()
    provider()
    a_room_refusal_parks_after_its_budget()
    a_logged_command_names_no_home_path()
    a_join_landing_on_the_third_send_delivers()
    another_403_keeps_its_handling_and_stays_bounded()
    return check.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
