#!/usr/bin/env python3
"""The proactive retry is BOUNDED by the shared send-failure policy.

Drives the PRODUCTION `_resolve_send_failure` (not a re-implementation): an
accepted-but-unconfirmed send retries at most MAX_TRANSIENT_ATTEMPTS times,
then parks to undeliverable/ — the 2026-08-16 incident (one nudge, 12 posts)
made the unbounded version a duplicate generator. Consolidates the #2959/#2960
collision: air's mechanism + verified-control methodology.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS += 1


def load_bridge(tmp: str):
    os.environ.update({
        "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": tmp,
        "REMOTE_TASK_URL": "http://127.0.0.1:1", "REMOTE_TASK_TOKEN": "t",
        "REMOTE_TASK_PROVIDER": "remote-gateway",
    })
    sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
    import importlib
    return importlib.import_module("ag2_sparrow.remote_gateway_bridge")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        rgb = load_bridge(tmp)
        from ag2_sparrow.send_failure_policy import UnconfirmedDelivery
        cap = rgb.MAX_TRANSIENT_ATTEMPTS
        results = Path(tmp) / "results"
        results.mkdir(parents=True, exist_ok=True)
        rgb.RESULTS_DIR = results
        undeliv = Path(tmp) / "results" / "archive" / "undeliverable"
        rgb.UNDELIVERABLE_RESULTS_DIR = undeliv
        rgb._PROACTIVE_ATTEMPTS.clear()

        # 1. bounded retry then park (the incident shape)
        body = results / "proactive-1.txt"
        outcomes = []
        for i in range(cap + 3):
            body.write_text("hi")
            claim = results / "proactive-1.sending"
            body.rename(claim)
            out = rgb._resolve_send_failure(
                claim, body, UnconfirmedDelivery("no event_id"))
            outcomes.append(out)
            if not body.exists():
                break
        retried = sum(1 for o in outcomes if o.startswith("will retry"))
        check(retried == cap,
              f"unconfirmed send retries exactly {cap} times, then stops")
        check(outcomes[-1].startswith("parked") or not body.exists(),
              "past the cap the outcome is park, not another retry")
        parked = undeliv / "proactive-1.sending"
        check(undeliv.exists() and any(undeliv.iterdir()),
              "parked file lands in undeliverable/ (recoverable by hand)")
        check(rgb._PROACTIVE_ATTEMPTS.get("proactive-1.txt") is None,
              "ledger entry cleared on park (no leak)")

        # 2. independent files count independently
        rgb._PROACTIVE_ATTEMPTS.clear()
        other = results / "proactive-2.txt"
        other.write_text("yo")
        claim2 = results / "proactive-2.sending"
        other.rename(claim2)
        out = rgb._resolve_send_failure(claim2, other, UnconfirmedDelivery("x"))
        check(out.startswith("will retry") and other.exists(),
              "a fresh file starts from a fresh count and is re-queued")

        # 3. VERIFIED CONTROL: the bound really is the policy's — raising the
        # cap must change behavior (fails if the helper ignores the policy).
        rgb._PROACTIVE_ATTEMPTS.clear()
        rgb._PROACTIVE_ATTEMPTS["proactive-2.txt"] = cap + 10
        other.rename(claim2)
        out = rgb._resolve_send_failure(claim2, other, UnconfirmedDelivery("x"))
        check(not other.exists() and not out.startswith("will retry"),
              "control: an over-cap count parks immediately (policy is live)")

        # 4. Production shapes: urllib WRAPPERS, not bare exceptions — a
        # wrapped transient must retry, a permanent 4xx must park at once.
        import socket
        import urllib.error
        for reason, label in [
            (TimeoutError("t"), "URLError(TimeoutError)"),
            (ConnectionRefusedError(), "URLError(ConnectionRefusedError)"),
            (socket.gaierror(8, "nodename nor servname"), "URLError(gaierror)"),
        ]:
            rgb._PROACTIVE_ATTEMPTS.clear()
            f = results / "proactive-net.txt"
            f.write_text("net")
            c = results / "proactive-net.sending"
            f.rename(c)
            out = rgb._resolve_send_failure(c, f, urllib.error.URLError(reason))
            check(out.startswith("will retry") and f.exists(),
                  f"{label} is transient: retried, not parked at attempt 0")
            f.unlink()

        rgb._PROACTIVE_ATTEMPTS.clear()
        f = results / "proactive-4xx.txt"
        f.write_text("gone")
        c = results / "proactive-4xx.sending"
        f.rename(c)
        http404 = urllib.error.HTTPError("http://x", 404, "nf", None, None)
        out = rgb._resolve_send_failure(c, f, http404)
        check(not f.exists() and not out.startswith("will retry"),
              "permanent 4xx parks on the first attempt (no useless retries)")

        # 5. wrapped transients respect the same ceiling
        rgb._PROACTIVE_ATTEMPTS.clear()
        rgb._PROACTIVE_ATTEMPTS["proactive-net.txt"] = cap
        f = results / "proactive-net.txt"
        f.write_text("net")
        c = results / "proactive-net.sending"
        f.rename(c)
        out = rgb._resolve_send_failure(
            c, f, urllib.error.URLError(TimeoutError("t")))
        check(not f.exists() and not out.startswith("will retry"),
              "a wrapped transient still parks once the cap is reached")

        # 6. the vendored resolve_failed_send is callable: it imports
        # proactive_recovery, which must ship in the package alongside it
        from ag2_sparrow.send_failure_policy import resolve_failed_send
        f = results / "proactive-pkg.txt"
        f.write_text("pkg")
        c = results / "proactive-pkg.sending"
        f.rename(c)
        out = resolve_failed_send(c, UnconfirmedDelivery("x"), {})
        check(out in ("retried", "parked"),
              f"packaged resolve_failed_send runs without ModuleNotFoundError ({out})")

    if FAILS:
        print(f"FAILED ({FAILS})")
        return 1
    print("PASS — proactive retries are bounded by the shared policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
