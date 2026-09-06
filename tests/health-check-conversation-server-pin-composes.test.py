#!/usr/bin/env python3
"""A HEALTHY conversation-server row must carry its pin, like every other adapter.

The non-ok branch re-applied the pin after rewriting check_port's diagnosis; the
healthy branch called only mark_stale_if_outdated(), which returns without
evaluating a pin when process and source are current. A pinned but healthy phone
process therefore shipped as plain `ok` and the renderer gave the owner no
DO NOT RESTART warning — the manual-restart surface the pin exists to protect.

Reciprocal controls: unpinned the row must stay `ok` with no veto (otherwise the
pinned assertion passes by construction), and the tunnel check that reads this
row's status must still run when a pin escalates ok -> warn.

STALENESS IS THE SECOND REWRITE, and this file stubbed `mark_stale_if_outdated`
to a no-op, so it could not see it: a LIVE but stale pinned server left
`_cs_live` False and the whole tunnel block was skipped, so a dead ngrok raised
no issue while inbound calls failed. The stale case below drives the real
rewrite and counts probes of port 4040.

Run: python3 tests/health-check-conversation-server-pin-composes.test.py
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-cs-pin-")
_ccd = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_ccd.mkdir(parents=True, exist_ok=True)
(_ccd / "access.json").write_text("{}")

REPO = Path(__file__).resolve().parents[1]
PID = "515151"
LSTART = "Mon Aug 25 00:00:00 2026"


def _load():
    spec = importlib.util.spec_from_file_location("hc_cs_pin", REPO / "src/health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(pinned: bool, stale: bool = False) -> tuple:
    """(status, detail, row, ngrok_probes) for the conversation-server row."""
    import sys
    sys.path.insert(0, str(REPO / "src"))
    import process_pins

    with tempfile.TemporaryDirectory() as td:
        ws, repo = Path(td) / "ws", Path(td) / "repo"
        (ws / "state").mkdir(parents=True)
        (repo / "src").mkdir(parents=True)
        env = repo / ".env"
        # No TWILIO_WEBHOOK_URL: the tunnel block is entered but does no network.
        # A tunnel URL so the ngrok probe is reachable — it is the observable.
        env.write_text("TWILIO_ACCOUNT_SID=ACtest\nTWILIO_AUTH_TOKEN=tok\n"
                       "TWILIO_WEBHOOK_URL=https://example.ngrok.io/hook\n")

        mod = _load()
        mod.WORKSPACE_DIR = ws
        mod.REPO_DIR = repo
        pin_file = ws / "state" / "process-pins.json"
        if pinned:
            process_pins.arm_pin(pin_file, "conversation-server", PID, LSTART,
                                 "branch-only witness in flight",
                                 "2099-01-01T00:00:00Z")

        mod._resolve_dotenv = lambda: env
        def _stale(check, *a, **k):
            if stale:
                check["status"] = "stale"
                check["detail"] = "running but code is newer than process"
        mod.mark_stale_if_outdated = _stale
        # Only this row's process seam moves; every other service's pins are
        # filtered out by name inside _pin_verdicts.
        mod._proc_lstarts = lambda pat: ([0.0], {PID: LSTART})

        real_port = mod.check_port
        probes = {"ngrok": 0}

        def port(p, name, *a, **k):
            if p == 4040:
                probes["ngrok"] += 1
                return {"name": "ngrok", "status": "down", "detail": "no tunnel"}
            if p == 3100:
                return {"name": "conversation-server", "status": "ok",
                        "detail": "port 3100", "live": True}
            return real_port(p, name, *a, **k)

        mod.check_port = port
        real_urlparse_guard = mod.config_get
        mod.config_get = lambda key, *a, **k: ("" if key == "SKIP_PHONE"
                                               else real_urlparse_guard(key, *a, **k))

        checks = mod.run_all_checks()
        row = next((c for c in checks if c.get("name") == "conversation-server"), None)
        assert row is not None, "conversation-server produced no check row"
        return row["status"], str(row.get("detail") or ""), row, probes["ngrok"]


# CONTROL FIRST: unpinned, a healthy row is plain ok with no veto anywhere.
status, detail, row, _n = _row(pinned=False)
assert status == "ok", f"control: healthy unpinned row should be ok, got {status}"
assert "DO NOT RESTART" not in detail, f"control carries a veto it should not: {detail}"
assert not row.get("restart_veto"), f"control carries restart_veto: {row}"

# PINNED: the same healthy row must surface the veto in BOTH places.
status, detail, row, n_pinned = _row(pinned=True)
assert status == "warn", f"pinned healthy row should escalate to warn, got {status}"
assert "DO NOT RESTART conversation-server pid " + PID in detail, (
    f"the owner-facing detail carries no veto: {detail}")
assert "DO NOT RESTART" in str(row.get("restart_veto") or ""), (
    f"restart_veto was not set, so --fix stays unprotected: {row}")
assert "port 3100" in detail, f"liveness detail was replaced, not composed: {detail}"

assert n_pinned >= 1, (
    f"a PIN escalation suppressed the tunnel probe (ngrok probes={n_pinned})")

# STALE: a LIVE but stale server must still have its tunnel diagnosed.
status, detail, row, n_stale = _row(pinned=False, stale=True)
assert n_stale >= 1, (
    f"a STALE live server suppressed the tunnel probe (ngrok probes={n_stale}) — "
    "a dead tunnel would raise no issue while inbound calls fail")
# Positive control: the same path with a fresh source must probe too, or the
# assertion above could pass for a reason unrelated to staleness.
_s, _d, _r, n_fresh = _row(pinned=False, stale=False)
assert n_fresh >= 1, f"control: fresh live server did not probe the tunnel ({n_fresh})"

print("PASS — a healthy conversation-server row composes its pin: veto reaches "
      "both restart_veto and the owner-facing detail; unpinned control stays ok; "
      "and the tunnel is probed under BOTH escalations (pin and staleness)")
