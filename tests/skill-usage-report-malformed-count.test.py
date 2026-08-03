#!/usr/bin/env python3
"""Behavioral suite for report-usage.py: malformed-record hardening + the
fail-open/always-exit-0 contract, through main() with stubbed collaborators.

Review lineage (#2180): three stuck-reporter findings, all the same class — a
raise AFTER log.rename(pending) exits nonzero and strands the .reporting claim:
  r2: pending-only crash recovery + chunked send (fixed upstream)
  r3: malformed persisted `count` parsed outside the guard
  r4: malformed `slug` shape (e.g. a list) raising unhashable at aggregation
The fix keeps the entire per-record parse+aggregate unit inside the guard; this
suite pins every degrade path so the class stays closed.

Run: python3 tests/skill-usage-report-malformed-count.test.py  (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --- stubs main() imports from src/: mutable so each case reconfigures them ---
class WsHolder:
    path = None
    raise_on_resolve = False


def _resolve_workspace():
    if WsHolder.raise_on_resolve:
        raise RuntimeError("no workspace helper (fallback case)")
    return str(WsHolder.path)


fake_ws_mod = types.ModuleType("workspace_default")
fake_ws_mod.resolve_workspace = _resolve_workspace
sys.modules["workspace_default"] = fake_ws_mod


class VaultHolder:
    ok = True


def _get_vault_key(key):
    if not VaultHolder.ok:
        raise KeyError(key)
    return "test-token"


fake_vault_mod = types.ModuleType("vault_intercept")
fake_vault_mod.get_vault_key = _get_vault_key
sys.modules["vault_intercept"] = fake_vault_mod

spec = importlib.util.spec_from_file_location(
    "report_usage", REPO / "skills" / "skill-usage-report" / "scripts" / "report-usage.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Urlopen:
    """Configurable fake: mode 'ok' returns accepted-all; 'fail' raises."""

    def __init__(self):
        self.mode = "ok"
        self.posted = []

    def __call__(self, req, timeout=None):
        if self.mode == "fail":
            raise OSError("network down (test)")
        self.posted.append(json.loads(req.data))
        resp = io.BytesIO(json.dumps({"accepted": len(json.loads(req.data)["events"]), "skipped": 0}).encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        return resp


urlopen = Urlopen()
mod.urllib.request.urlopen = urlopen


def run_case(records=None, *, mxid=True, vault=True, mode="ok", pre_pending=None,
             raw_lines=None, empty_log=False, no_log=False, ws_fallback=False):
    """Fresh temp workspace + configured stubs → mod.main() → (rc, ws)."""
    ws = Path(tempfile.mkdtemp())
    (ws / "state").mkdir()
    log = ws / "state" / "skill-usage-log.jsonl"
    if raw_lines is not None:
        log.write_text("".join(raw_lines))
    elif records is not None:
        log.write_text("".join(json.dumps(r) + "\n" for r in records))
    elif empty_log:
        log.write_text("")
    if pre_pending is not None:
        (ws / "state" / "skill-usage-log.jsonl.reporting").write_text(
            "".join(json.dumps(r) + "\n" for r in pre_pending))
    WsHolder.path = ws
    WsHolder.raise_on_resolve = ws_fallback
    VaultHolder.ok = vault
    os.environ.pop("AGENT_MXID", None)
    if mxid:
        os.environ["AGENT_MXID"] = "@test.agent:ag2.space"
    urlopen.mode = mode
    urlopen.posted.clear()
    rc = mod.main()
    WsHolder.raise_on_resolve = False
    return rc, ws


def events_by_slug():
    return {e["slug"]: e for chunk in urlopen.posted for e in chunk["events"]}


def claim(ws):
    return (ws / "state" / "skill-usage-log.jsonl.reporting").exists()


def active(ws):
    return (ws / "state" / "skill-usage-log.jsonl").exists()


# 1. r4 regression: malformed slug SHAPES must not strand the claim.
rc, ws = run_case([
    {"slug": ["bad", "list"], "ts": 100},
    {"slug": "", "ts": 100},
    {"slug": {"k": "v"}, "ts": 100, "count": 2},
    {"slug": "good", "ts": 200},
])
check("malformed slug shapes: exit 0", rc == 0, f"rc={rc}")
check("malformed slug shapes: skipped, valid slug still sent",
      set(events_by_slug()) == {"good"}, f"got {set(events_by_slug())}")
check("malformed slug shapes: claim released + log drained", not claim(ws) and not active(ws))

# 2. r3 regression: malformed persisted count → default 1, kept.
rc, ws = run_case([
    {"slug": "bad-count", "ts": 100, "count": "not-int"},
    {"slug": "good", "ts": 200, "count": 2},
    {"slug": "good", "ts": 300, "count": 3},
])
check("malformed count: exit 0", rc == 0, f"rc={rc}")
check("malformed count: kept with default 1", events_by_slug().get("bad-count", {}).get("count") == 1)
check("well-formed counts aggregate (2+3=5)", events_by_slug().get("good", {}).get("count") == 5)
check("malformed count: claim released", not claim(ws))

# 3. r2 regression: pending-only leftover (crash between rename and fold-back).
rc, ws = run_case(None, pre_pending=[{"slug": "recovered", "ts": 50}])
check("pending-only recovery: exit 0", rc == 0, f"rc={rc}")
check("pending-only recovery: leftover event reported", "recovered" in events_by_slug())
check("pending-only recovery: claim released", not claim(ws))

# 4. pending + active both present → merged, nothing lost.
rc, ws = run_case([{"slug": "fresh", "ts": 300}], pre_pending=[{"slug": "stale", "ts": 100}])
check("pending+active merge: both reported", set(events_by_slug()) == {"fresh", "stale"},
      f"got {set(events_by_slug())}")

# 5. empty log → nothing to report, no claim taken.
rc, ws = run_case(None, empty_log=True)
check("empty log: exit 0, no POST, no claim", rc == 0 and not urlopen.posted and not claim(ws))

# 6. AGENT_MXID unset → skip, log KEPT (no claim).
rc, ws = run_case([{"slug": "x", "ts": 1}], mxid=False)
check("no AGENT_MXID: exit 0, log kept, no claim", rc == 0 and active(ws) and not claim(ws))

# 7. vault token missing → skip, log KEPT.
rc, ws = run_case([{"slug": "x", "ts": 1}], vault=False)
check("no vault token: exit 0, log kept, no claim", rc == 0 and active(ws) and not claim(ws))

# 8. log with only garbage lines → parsed to zero events, claim released.
rc, ws = run_case(raw_lines=["not json at all\n", '{"ts": 1}\n', '{"slug":"x","ts":"NaN"}\n'])
check("unparseable-only log: exit 0, no POST, claim released",
      rc == 0 and not urlopen.posted and not claim(ws) and not active(ws))

# 9. POST failure → fold_back persists count-carrying records; retry drains them.
rc, ws = run_case([{"slug": "a", "ts": 100}, {"slug": "a", "ts": 200}, {"slug": "b", "ts": 300}], mode="fail")
check("POST failure: exit 0 (fail-open)", rc == 0, f"rc={rc}")
check("POST failure: claim released, remainder folded into ACTIVE log", not claim(ws) and active(ws))
folded = [json.loads(x) for x in (ws / "state" / "skill-usage-log.jsonl").read_text().splitlines()]
check("fold_back writes count-carrying records", any(r.get("slug") == "a" and r.get("count") == 2 for r in folded),
      f"folded={folded}")
# retry against the SAME workspace: folded-back log drains cleanly on success.
WsHolder.path = ws
urlopen.mode = "ok"
urlopen.posted.clear()
rc2 = mod.main()
check("retry after fold_back: exit 0, aggregate intact (a:2, b:1)",
      rc2 == 0 and events_by_slug().get("a", {}).get("count") == 2
      and events_by_slug().get("b", {}).get("count") == 1 and not active(ws) and not claim(ws))

# 10. workspace-helper import failure → fallback path; with no log there it
# degrades to "nothing to report" without writing anything.
rc, _ = run_case(None, no_log=True, ws_fallback=True)
check("workspace fallback: exit 0, no POST", rc == 0 and not urlopen.posted)

# 11. RACE regression (async-hook era): a hook append landing at the most
# adversarial recovery moment — just as the stale claim is being released —
# must survive and be reported. The old read→unlink→rename recovery destroyed
# such appends (or clobbered a fresh log); the append-into-active direction
# never unlinks/renames the active log, so the raced record stays. Under the
# old code this patch fires at final claim-release instead and the raced
# record misses the report — the case discriminates the two behaviors.
import pathlib

_real_unlink = pathlib.Path.unlink
_race = {"fired": False}


def _racing_unlink(self, *a, **k):
    if self.name.endswith(".reporting") and not _race["fired"]:
        _race["fired"] = True
        with (self.parent / "skill-usage-log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"slug": "raced", "ts": 999}) + "\n")
    return _real_unlink(self, *a, **k)


pathlib.Path.unlink = _racing_unlink
try:
    rc, ws = run_case([{"slug": "fresh", "ts": 300}], pre_pending=[{"slug": "stale", "ts": 100}])
finally:
    pathlib.Path.unlink = _real_unlink
check("recovery race: exit 0", rc == 0, f"rc={rc}")
check("recovery race: concurrent hook append SURVIVES recovery and is reported",
      set(events_by_slug()) >= {"raced", "stale", "fresh"}, f"got {set(events_by_slug())}")
check("recovery race: claim released, log drained", not claim(ws) and not active(ws))

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — reporter degrade paths pinned; stuck-claim class closed")
