#!/usr/bin/env python3
"""Allowlist-divergence warning in the ag2-sparrow gateway bridge.

The broker registry decides whose room messages become tasks; local access.json
only re-tiers ones that arrived. A local-only add therefore drops silently at
the broker, and the bridge detects that divergence and warns the owner.

Hermetic — no network: _req is monkeypatched; config/state under temp roots
(CLAUDE_CONFIG_DIR seeded BEFORE import per the #2429 isolation rule).

Covers:
  1. pure divergence body: missing sender named + exact fix command; None when aligned
  2. own agent-id / blank entries never count as divergence
  3. hook end-to-end: local access.json + fake broker → ONE [dm-only] proactive file
  4. dedup: unchanged divergence never warns twice; a NEW divergence re-warns
  5. broker read failure → no warning, no mtime consumption (retries next loop)
  6. realignment clears the warned-hash (future divergence warns again)
  7. a persistently failing broker keeps retrying but logs once per cooldown

Run: python3 tests/gateway-allowlist-divergence.test.py   (exit 0 / 1)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolate channel config BEFORE the bridge import (#2429 rule): access path +
# agent identity both resolve under this temp root, never the operator's real one.
_CFG = Path(tempfile.mkdtemp(prefix="allowdiv-cfg-"))
os.environ["CLAUDE_CONFIG_DIR"] = str(_CFG)
_CH = _CFG / "channels" / "ag2space"
_CH.mkdir(parents=True, exist_ok=True)
(_CH / ".env").write_text("AGENT_ID=@sutando-test:ag2.space\n")
os.environ.pop("AGENT_ID", None)
os.environ.pop("AG2_DEVICE_ENV", None)

sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402
from ag2_sparrow._dirs import set_dirs  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


AGENT = "@sutando-test:ag2.space"

# ---------------------------------------------------------------------------
# 1–2. pure body
# ---------------------------------------------------------------------------
body = rgb.allowlist_divergence(["@rui:hs", "@mark:hs"], ["@rui:hs"], AGENT)
check("divergence names the missing sender", body is not None and "@mark:hs" in body, repr(body)[:80])
check("divergence carries the exact fix command",
      body is not None and f"agent_access.py set {AGENT} --allow-add @mark:hs" in body, repr(body)[-120:])
check("divergence is dm-only", body is not None and body.startswith("[dm-only]"))
check("aligned → None", rgb.allowlist_divergence(["@rui:hs"], ["@rui:hs"], AGENT) is None)
check("broker superset → None (only silent-drop direction warns)",
      rgb.allowlist_divergence(["@rui:hs"], ["@rui:hs", "@extra:hs"], AGENT) is None)
check("own id/blank entries ignored",
      rgb.allowlist_divergence([AGENT, "  ", "@rui:hs"], ["@rui:hs"], AGENT) is None)

# ---------------------------------------------------------------------------
# hook end-to-end with fake broker + temp dirs
# ---------------------------------------------------------------------------
def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="allowdiv-ws-"))
    set_dirs(task_dir=tmp / "tasks", result_dir=tmp / "results", state_dir=tmp / "state")
    rgb.RESULTS_DIR = tmp / "results"
    rgb._ALLOWDIV_STATE["mtime"] = None
    rgb._ALLOWDIV_STATE["warned_hash"] = None
    return tmp


def write_access(allow):
    p = Path(rgb._ag2space_access_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"allowFrom": allow}))
    # mtime granularity is coarse — force a distinct value per write
    t = time.time() + write_access._bump
    write_access._bump += 2
    os.utime(p, (t, t))


write_access._bump = 0

_orig_req = rgb._req


def fake_req_factory(agents_payload=None, fail=False):
    def _fake(method, path, payload=None, timeout=35):
        if path == "/v1/agents":
            if fail:
                raise OSError("broker down")
            return {"agents": agents_payload or []}
        raise AssertionError(f"unexpected {method} {path}")
    return _fake


def proactive_files(tmp):
    return sorted((tmp / "results").glob("proactive-allowdiv-*.txt"))


# 3. divergence → exactly one [dm-only] proactive file
tmp = fresh()
write_access(["@rui:hs", "@mark:hs"])
rgb._req = fake_req_factory([{"id": AGENT, "allowFrom": ["@rui:hs"]}])
try:
    rgb._maybe_warn_allowlist_divergence()
    files = proactive_files(tmp)
    check("hook writes one proactive warning", len(files) == 1, repr(files))
    body = files[0].read_text() if files else ""
    check("warning body has sender + command",
          "@mark:hs" in body and "agent_access.py set" in body, body[:100])

    # 4a. same divergence, another loop pass → no second file
    rgb._maybe_warn_allowlist_divergence()
    check("unchanged divergence not re-warned", len(proactive_files(tmp)) == 1)

    # 4b. access.json touched but SAME divergence set → still no second file
    write_access(["@rui:hs", "@mark:hs"])
    rgb._maybe_warn_allowlist_divergence()
    check("same set after touch not re-warned (hash dedup)", len(proactive_files(tmp)) == 1)

    # 4b-bis. BROKER-ONLY transitions with access.json untouched — the case an
    # mtime-only trigger cannot see. Each pass forces the poll due, not sleeps.
    def _due():
        rgb._ALLOWDIV_STATE["next_poll"] = 0.0

    before = len(proactive_files(tmp))
    mtime_before = os.path.getmtime(rgb._ag2space_access_path())

    # broker realigns (adds the missing sender) — warned_hash must clear
    rgb._req = fake_req_factory([{"id": AGENT, "allowFrom": ["@rui:hs", "@mark:hs"]}])
    _due(); rgb._maybe_warn_allowlist_divergence()
    check("broker-only realignment writes no new warning",
          len(proactive_files(tmp)) == before)
    check("broker-only realignment CLEARED the warned hash",
          rgb._ALLOWDIV_STATE["warned_hash"] is None)

    # broker diverges again — must re-warn without any local change
    rgb._req = fake_req_factory([{"id": AGENT, "allowFrom": ["@rui:hs"]}])
    _due(); rgb._maybe_warn_allowlist_divergence()
    check("broker-only re-divergence re-warns", len(proactive_files(tmp)) == before + 1,
          repr(proactive_files(tmp)))
    check("access.json was never touched across the three passes",
          os.path.getmtime(rgb._ag2space_access_path()) == mtime_before)

    # 4c. a NEW divergence (another sender) → one more warning. Counted relative
    # to the prior state: an absolute count silently breaks when a case is inserted
    # above, which is a stale assertion rather than a real regression.
    n_before = len(proactive_files(tmp))
    write_access(["@rui:hs", "@mark:hs", "@sam:hs"])
    rgb._maybe_warn_allowlist_divergence()
    check("new divergence set re-warns", len(proactive_files(tmp)) == n_before + 1,
          repr(proactive_files(tmp)))
finally:
    rgb._req = _orig_req

# 5. broker read failure → no warning, retried (mtime not consumed)
tmp = fresh()
write_access(["@rui:hs", "@mark:hs"])
rgb._req = fake_req_factory(fail=True)
try:
    rgb._maybe_warn_allowlist_divergence()
    check("broker failure → no warning", len(proactive_files(tmp)) == 0)
    # now broker comes back — same mtime must be retried
    rgb._req = fake_req_factory([{"id": AGENT, "allowFrom": ["@rui:hs"]}])
    rgb._maybe_warn_allowlist_divergence()
    check("retry after broker recovery warns", len(proactive_files(tmp)) == 1)
finally:
    rgb._req = _orig_req

# 6. realignment clears warned-hash → later divergence warns again
tmp = fresh()
write_access(["@rui:hs", "@mark:hs"])
rgb._req = fake_req_factory([{"id": AGENT, "allowFrom": ["@rui:hs"]}])
try:
    rgb._maybe_warn_allowlist_divergence()
    write_access(["@rui:hs"])                      # aligned now
    rgb._maybe_warn_allowlist_divergence()
    write_access(["@rui:hs", "@mark:hs"])          # diverges again
    rgb._maybe_warn_allowlist_divergence()
    check("realign then re-diverge warns twice total", len(proactive_files(tmp)) == 2,
          repr(proactive_files(tmp)))
finally:
    rgb._req = _orig_req

# 7. unsupported endpoint (404/405 = pre-registry gateway): mirror the /ack
# cooldown (qingyun CR 2026-07-30). Two consecutive loops must make exactly ONE
# /v1/agents attempt and ONE log line; after the cooldown expires the request
# is retried (self-healing, no worker restart needed). mtime stays unmarked so
# a gateway that gains the endpoint still gets the real divergence check.
import io
import urllib.error

tmp = fresh()
rgb._ALLOWDIV_STATE["unsupported_until"] = 0.0
write_access(["@rui:hs", "@mark:hs"])
_404_calls = {"n": 0}
_404_logs: list[str] = []


def fake_404(method, path, payload=None, timeout=35):
    _404_calls["n"] += 1
    raise urllib.error.HTTPError("/v1/agents", 404, "Not Found", {}, io.BytesIO(b""))


_orig_log = rgb._log
rgb._req = fake_404
rgb._log = lambda msg: _404_logs.append(msg)
try:
    rgb._maybe_warn_allowlist_divergence()
    rgb._maybe_warn_allowlist_divergence()
    check("404: two loops make exactly one /v1/agents attempt",
          _404_calls["n"] == 1, f"calls={_404_calls['n']}")
    check("404: exactly one log line (no per-loop spam)",
          len([m for m in _404_logs if "unsupported" in m]) == 1
          and len(_404_logs) == 1, repr(_404_logs))
    check("404: no warning files written", len(proactive_files(tmp)) == 0,
          repr(proactive_files(tmp)))
    check("404: mtime left unmarked (endpoint may appear later)",
          rgb._ALLOWDIV_STATE["mtime"] is None, repr(rgb._ALLOWDIV_STATE))
    # cooldown expiry → retried exactly once more
    rgb._ALLOWDIV_STATE["unsupported_until"] = time.time() - 1
    rgb._maybe_warn_allowlist_divergence()
    check("404: retried after the cooldown (self-healing)",
          _404_calls["n"] == 2, f"calls={_404_calls['n']}")
    # broker later GAINS the endpoint → divergence detection works immediately
    rgb._ALLOWDIV_STATE["unsupported_until"] = 0.0
    rgb._req = fake_req_factory([{"id": AGENT, "allowFrom": ["@rui:hs"]}])
    rgb._maybe_warn_allowlist_divergence()
    check("gained endpoint after cooldown: divergence warns",
          len(proactive_files(tmp)) == 1, repr(proactive_files(tmp)))
finally:
    rgb._req = _orig_req
    rgb._log = _orig_log
    rgb._ALLOWDIV_STATE["unsupported_until"] = 0.0

# 7b. a non-404/405 HTTP error (e.g. 500) stays TRANSIENT: no cooldown is
# entered, so the very next loop retries — only the unsupported-endpoint
# shape gets the time gate.
tmp = fresh()
rgb._ALLOWDIV_STATE["unsupported_until"] = 0.0
write_access(["@rui:hs", "@mark:hs"])
_500_calls = {"n": 0}


def fake_500(method, path, payload=None, timeout=35):
    _500_calls["n"] += 1
    raise urllib.error.HTTPError("/v1/agents", 500, "boom", {}, io.BytesIO(b""))


rgb._req = fake_500
_500_logs = []
rgb._log = _500_logs.append
try:
    for _ in range(5):
        rgb._maybe_warn_allowlist_divergence()
    check("500: stays transient — every loop retries, no cooldown",
          _500_calls["n"] == 5 and rgb._ALLOWDIV_STATE["unsupported_until"] == 0.0,
          f"calls={_500_calls['n']} state={rgb._ALLOWDIV_STATE}")
    # Retries are every-loop by design, so the log is what has to be bounded.
    check("500: five failures log ONCE, not five times",
          len(_500_logs) == 1, f"logs={_500_logs}")
    rgb._ALLOWDIV_STATE["fail_log_until"] = 0.0
    rgb._maybe_warn_allowlist_divergence()
    check("500: a failure after the window logs again",
          len(_500_logs) == 2, f"logs={_500_logs}")
finally:
    rgb._req = _orig_req
    rgb._log = _orig_log

# agent-id resolution from the seeded .env (no $AGENT_ID in env)
check("agent id read from channel .env", rgb._agent_id() == AGENT, rgb._agent_id())

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All gateway allowlist-divergence checks passed.")
