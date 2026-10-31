#!/usr/bin/env python3
"""The status-ack runner's and notify's fail-closed / fail-open edges.

  a) the probe cannot answer -> nothing is said, nothing is written
  b) a result being sent, or a pending cancel, seen at observation -> no line
  c) a task file gone between the probe and the read -> not considered
  d) the dispatch gate raises during the publish-time re-check -> withdrawn, claim released
  e) the claim is lost between decide and claim -> the loser publishes nothing
  f) a published line with no outbox item yet stays claimed across passes
  g) --loop repeats passes while enabled; an unreadable manifest is empty config
  h) notify: a claim store that raises never blocks a send, and a failed settle keeps rc 0
  i) the proactive item id has one owner: the bridge's id is src/proactive_routing's

Run: python3 tests/status-ack-edges.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402
import proactive_routing  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ISO = pl.scratch("status-ack-edges-")
check = pl.Checks()
mod = sa.runner()
TID = "task-1761300000001"
NOW = sa.T0 + 130


@contextlib.contextmanager
def patched(obj, name, value):
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


def fresh():
    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    return lane


def run(lane, **kw):
    return mod.run_once(lane.ws, sa.on(mod), now=kw.pop("now", NOW), holders=sa.SESSION, **kw)


def main() -> int:
    lane = fresh()

    def broken_probe():
        raise ImportError("no probe")

    with patched(mod, "_load_probe", broken_probe):
        r = run(lane)
    check(r.skipped.get("*") == "the probe could not answer" and sa.proactive_files(lane.ws) == [],
          f"a) no probe -> nothing said ({r.skipped})")

    lane = fresh()
    (lane.results / f"{TID}.txt.sending.1").write_text("x", encoding="utf-8")
    r = run(lane)
    check(not r.spoken and "sending" in r.skipped.get(TID, ""), f"b) a result being sent -> no line ({r.skipped})")
    lane = fresh()
    (lane.tasks / "task-1761300009999.txt").write_text(
        f"id: task-1761300009999\ntask: CANCEL_INSTRUCTION: stop processing {TID}\n", encoding="utf-8")
    r = run(lane)
    check(not r.spoken and r.skipped.get(TID) == "cancelled", f"b) a pending cancel -> no line ({r.skipped})")

    lane = fresh()
    obs, why = mod.observe(lane.ws, {"task_id": "task-1761300000077"}, "session", False)
    check(obs is None and why == "the task left the queue", f"c) a vanished file is not considered ({why})")

    def raising_gate(*a, **k):
        raise RuntimeError("gate down")

    lane = fresh()
    with patched(mod, "dispatch_veto", raising_gate):
        r = run(lane)
    check(sa.proactive_files(lane.ws) == [] and "could not be checked" in r.withdrawn.get(TID, "")
          and ack_claim.state(lane.ws, f"ack:{TID}:initial") == "none",
          f"d) a raising gate withdraws and releases ({r.withdrawn})")

    lane = fresh()
    with patched(mod.ack_claim, "claim", lambda *a, **k: False):
        r = run(lane)
    check(not r.spoken and r.skipped.get(TID) == "another sender holds the initial ack" and
          sa.proactive_files(lane.ws) == [], f"e) a lost claim publishes nothing ({r.skipped})")

    lane = fresh()
    run(lane)
    r = run(lane, now=NOW + 15)
    check(r.settled == [] and ack_claim.state(lane.ws, f"ack:{TID}:initial") == "claimed",
          f"f) no outbox item yet -> still claimed ({r.settled})")

    sleeps, real_sleep = [], mod.time.sleep

    def stop(seconds):
        if seconds != 15.0:
            return real_sleep(seconds)
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    lane = fresh()
    out = io.StringIO()
    with patched(mod.time, "sleep", stop), contextlib.redirect_stdout(out):
        try:
            mod.main(["--workspace", str(lane.ws), "--enabled", "1", "--loop"])
        except KeyboardInterrupt:
            pass
    check(len(out.getvalue().splitlines()) == 2 and sleeps == [15.0, 15.0],
          f"g) --loop runs a pass per poll ({len(out.getvalue().splitlines())} passes, sleeps {sleeps})")
    check(mod.manifest_config(ISO / "no-such-manifest.json") == {}, "g) an unreadable manifest is empty config")

    spec = importlib.util.spec_from_file_location("notify_edges", REPO / "skills" / "task-progress" / "scripts" / "notify.py")
    notify = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = notify
    spec.loader.exec_module(notify)
    posts = []
    notify._post = lambda url, payload, headers, timeout=10: posts.append(payload) or True

    class Raising:
        @staticmethod
        def claim(*a, **k):
            raise OSError("read-only")

        @staticmethod
        def update(*a, **k):
            raise OSError("read-only")

    lane = fresh()
    tf = lane.tasks / f"{TID}.txt"
    argv = ["notify.py", "--task-file", str(tf), "--message", "On it."]
    with patched(notify, "_ACK_ARBITRATION", (Raising, *notify._ACK_ARBITRATION[1:])), patched(sys, "argv", argv):
        rc = notify.main()
    check(rc == 0 and len(posts) == 1, f"h) a raising claim store never blocks the send (rc {rc}, {len(posts)})")

    class SettleFails:
        claim = staticmethod(lambda *a, **k: True)
        update = Raising.update

    lane = fresh()
    argv = ["notify.py", "--task-file", str(lane.tasks / f"{TID}.txt"), "--message", "On it."]
    with patched(notify, "_ACK_ARBITRATION", (SettleFails, *notify._ACK_ARBITRATION[1:])), patched(sys, "argv", argv):
        rc = notify.main()
    check(rc == 0 and len(posts) == 2, f"h) a failed settle after a good send keeps rc 0 (rc {rc})")

    check(gb._proactive_item_id("proactive-x", "body") == proactive_routing.proactive_item_id("proactive-x", "body")
          and str(gb._proactive_outbox_root()).endswith(proactive_routing.PROACTIVE_OUTBOX_DIRNAME),
          "i) the bridge's item id and outbox dir come from proactive_routing")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
