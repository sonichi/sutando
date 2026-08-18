#!/usr/bin/env python3
"""The non-owner result guard: one implementation, and it runs before markers.

Removing the Team provider session moved Team work onto the direct-core path,
which took the final secret and delivery-marker scan with it — the scan lived
inside the provider runner, so nothing reached it once the runner was bypassed.
The policy now lives in src/team_result_guard.py and the routers call it.

Part 1 (BEHAVIORAL) drives the guard through owner-passthrough, every withheld
case, and a scanner that raises — the last one because "unscannable" must fail
CLOSED, and a guard only ever observed passing is not a validated guard.

Part 2 (STRUCTURAL) pins what a behavioral test cannot reach: that the guard is
called BEFORE parse_markers in the delivery loop (ordering is the whole point —
a scan after the router has already read a redirect is decoration), and that no
consumer redefines the policy it is supposed to import.

Run: python3 tests/team-result-guard.test.py
Exit code: 0 on pass, 1 on fail.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import team_result_guard as guard  # noqa: E402

BRIDGE = REPO / "src" / "discord-bridge.py"
WORKER = REPO / "skills" / "task-workstream-sessions" / "scripts" / "session-worker.py"


class _Scan:
    def __init__(self, detected, types_=()):
        self.detected = detected
        self.secret_types = list(types_)


def _clean(_body):
    return _Scan(False)


def _leaky(_body):
    return _Scan(True, ["aws_key"])


def _raises(_body):
    raise RuntimeError("scanner down")


def behavioral() -> list:
    fails = []
    cases = [
        # name, body, tier, filter, expect_withheld — guard width tracks
        # parse_markers per family (anchored vs anywhere), matching consumers.
        ("owner keeps its markers", "[channel: 123]\nsee this", "owner", _clean, False),
        ("owner keeps an attach", "[attach: /tmp/x.png]", "owner", _clean, False),
        ("team redirect (body start) withheld", "[channel: 123]\nthe reply", "team", _clean, True),
        ("team redirect mid-prose is a MENTION, passes", "see [channel: 123]", "team", _clean, False),
        ("team attach withheld", "[attach: /etc/passwd]", "team", _clean, True),
        ("team attach mid-prose STILL withheld (unanchored family)",
         "docs mention [file: /etc/passwd] here", "team", _clean, True),
        ("team no-send (body start) withheld", "[no-send]", "team", _clean, True),
        ("team no-send mid-prose is a MENTION, passes",
         "the [no-send] marker suppresses delivery", "team", _clean, False),
        ("team deduped mid-prose passes", "about [deduped: task-1] semantics", "team", _clean, False),
        ("team dm-only ANYWHERE withheld (fail-safe family)",
         "prose then [dm-only] later", "team", _clean, True),
        ("team secret withheld", "ordinary text", "team", _leaky, True),
        ("team clean text passes", "ordinary text", "team", _clean, False),
        ("guest guarded like team", "[channel: 9]\nx", "guest", _clean, True),
        ("unknown tier guarded", "[channel: 9]\nx", "", _clean, True),
        ("None tier guarded", "[channel: 9]\nx", None, _clean, True),
    ]
    for name, body, tier, filt, expect in cases:
        out, why = guard.guard_result_for_tier(body, tier, REPO, secret_filter=filt)
        withheld = why is not None
        if withheld != expect:
            fails.append(f"{name}: expected withheld={expect}, got {withheld} ({why})")
            continue
        if withheld and out != guard.TEAM_LEAK_RESULT:
            fails.append(f"{name}: withheld but body was not the leak sentinel")
        if not withheld and out != body:
            fails.append(f"{name}: passed but body was altered")

    # An unscannable result must be withheld, never delivered on the assumption
    # that it was probably fine.
    out, why = guard.guard_result_for_tier("x", "team", REPO, secret_filter=_raises)
    if out != guard.TEAM_LEAK_RESULT or not why:
        fails.append("a raising scanner must fail CLOSED and withhold the body")

    # The caller is handed only the safe body — it cannot deliver the raw text
    # by swallowing an exception.
    out, _ = guard.guard_result_for_tier("[channel: 5]\nsecret", "team", REPO, secret_filter=_clean)
    if "[channel:" in out:
        fails.append("the withheld body still carried the control marker")

    if guard.is_guarded_tier("owner"):
        fails.append("owner must not be guarded")
    for tier in ("team", "guest", "other", "ambient", "", None, "OWNERISH"):
        if not guard.is_guarded_tier(tier):
            fails.append(f"tier {tier!r} must be guarded (only exact 'owner' is exempt)")
    if not guard.is_guarded_tier("Owner "):
        pass  # case/space-insensitive owner is intentionally exempt

    # Any body the consumer grammar acts on must be withheld for a guarded
    # tier, over every parser family — new families cannot bypass the guard.
    from result_markers import parse_markers
    family_uses = {
        "skip": "[no-send]\nbody",
        "redirect": "[channel: 123]\nbody",
        "attach": "mid prose [file: /tmp/f] ok",
        "dm-only": "prose [dm-only] prose",
    }
    for family, body in family_uses.items():
        acted = [a.kind for a in parse_markers(body).actions]
        if not acted:
            fails.append(f"invariant fixture stale: {family} body yields no consumer action")
            continue
        _, why = guard.guard_result_for_tier(body, "team", REPO, secret_filter=_clean)
        if why is None:
            fails.append(f"consumer acts on {family} but the guard passed it")

    # Withheld bodies are persisted for owner review — the placeholder's
    # claim must be TRUE (pre-fix the body was silently dropped).
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.environ["SUTANDO_WORKSPACE_FOR_TEST"] = td
        import workspace_default as wd
        real_resolve = wd.resolve_workspace
        wd.resolve_workspace = lambda *a, **kw: Path(td)
        try:
            secret_body = "[channel: 5]\nthe withheld payload"
            out, why = guard.guard_result_for_tier(secret_body, "team", REPO, secret_filter=_clean)
            saved = list((Path(td) / "state" / "withheld-team-results").glob("withheld-*.txt"))
            if len(saved) != 1:
                fails.append(f"withheld body was not persisted (found {len(saved)})")
            else:
                content = saved[0].read_text()
                if "the withheld payload" not in content or "withheld_reason:" not in content:
                    fails.append("persisted file missing body or reason header")
                if (saved[0].stat().st_mode & 0o777) != 0o600:
                    fails.append("persisted withheld file must be 0600")
            if "withheld-team-results" not in out:
                fails.append("placeholder must name the review location")
        finally:
            wd.resolve_workspace = real_resolve
            os.environ.pop("SUTANDO_WORKSPACE_FOR_TEST", None)

    # Two same-process same-ms withholds must BOTH persist; a persist
    # failure must switch to the no-copy variant, never claim "saved".
    import time as _time
    with tempfile.TemporaryDirectory() as td:
        import workspace_default as wd2
        real_resolve2 = wd2.resolve_workspace
        wd2.resolve_workspace = lambda *a, **kw: Path(td)
        real_time = _time.time
        _time.time = lambda: 1700000000.123  # frozen clock: same ms for both
        try:
            out1, _ = guard.guard_result_for_tier("[no-send]\nbody one", "team", REPO, secret_filter=_clean)
            out2, _ = guard.guard_result_for_tier("[no-send]\nbody two", "team", REPO, secret_filter=_clean)
            saved = list((Path(td) / "state" / "withheld-team-results").glob("withheld-*.txt"))
            if len(saved) != 2:
                fails.append(f"same-ms withholds must both persist (found {len(saved)})")
            if out1 != guard.TEAM_LEAK_RESULT or out2 != guard.TEAM_LEAK_RESULT:
                fails.append("both same-ms placeholders must claim the saved copy")
        finally:
            _time.time = real_time
            wd2.resolve_workspace = real_resolve2

    # Persistence failure -> honest placeholder, body still withheld.
    import workspace_default as wd3
    real_resolve3 = wd3.resolve_workspace
    def _boom():
        raise RuntimeError("no workspace")
    wd3.resolve_workspace = _boom
    try:
        out, why = guard.guard_result_for_tier("[no-send]\nsecret", "team", REPO, secret_filter=_clean)
        if out != guard.TEAM_LEAK_RESULT_UNSAVED:
            fails.append("persist failure must use the no-copy-exists placeholder")
        if "secret" in out:
            fails.append("persist failure must never release the body")
        if why is None:
            fails.append("persist failure must still report withheld")
    finally:
        wd3.resolve_workspace = real_resolve3
    return fails


def structural() -> list:
    fails = []
    bridge = BRIDGE.read_text()
    worker = WORKER.read_text()

    if "from team_result_guard import" not in bridge:
        fails.append("discord-bridge must import the shared guard")

    # Ordering is the requirement: a scan that runs after the router has read a
    # redirect or an attachment path has already lost.
    call = bridge.find("guard_result_for_tier(reply_text")
    markers = bridge.find("_parsed = parse_markers(reply_text)")
    if call < 0:
        fails.append("discord-bridge must call guard_result_for_tier on the reply body")
    elif markers < 0:
        fails.append("could not locate the parse_markers call in the delivery loop")
    elif call > markers:
        fails.append("the guard must run BEFORE parse_markers, not after")

    # An unknown tier must be re-derived from the durable task file: the
    # in-memory tier map is not restored on restart.
    window = bridge[max(0, call - 800):markers] if call > 0 else ""
    if "_resolve_task_tier" not in window:
        fails.append("an unknown tier must be resolved from the task file before guarding")

    # One implementation: consumers import the policy, never restate it.
    for name, src, label in ((BRIDGE.name, bridge, "bridge"), (WORKER.name, worker, "worker")):
        if re.search(r"^\s*TEAM_RESULT_CONTROL\s*=\s*re\.compile", src, re.M):
            fails.append(f"{name} redefines TEAM_RESULT_CONTROL instead of importing it")
        if re.search(r"^class TeamResultLeakError", src, re.M):
            fails.append(f"{name} redefines TeamResultLeakError instead of importing it")
        if re.search(r"^def resolve_access_tier", src, re.M):
            fails.append(f"{name} redefines resolve_access_tier instead of importing it")
    if "from team_result_guard import" not in worker:
        fails.append("session-worker must import the shared guard")
    return fails


def packaged() -> list:
    """The bundled copy must work with ONLY packages/ag2-sparrow on sys.path.

    The suite's own `sys.path.insert(src)` is what masked the bare-import
    failure: every in-process call runs in a context a packaged deployment
    never has. So this check runs in a subprocess whose path has no src/."""
    import subprocess
    fails = []
    pkg = REPO / "packages" / "ag2-sparrow"
    code = (
        "import sys, types\n"
        f"sys.path.insert(0, {str(pkg)!r})\n"
        "assert not any(p.endswith('/src') for p in sys.path), 'src leaked onto path'\n"
        "from ag2_sparrow.team_result_guard import scan_team_result\n"
        "scan = lambda b: types.SimpleNamespace(detected=False, secret_types=[])\n"
        "body = 'a clean team answer with no markers'\n"
        "out = scan_team_result(body, None, secret_filter=scan)\n"
        "assert out == body, f'clean body was altered or withheld: {out!r}'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd="/")
    if proc.returncode != 0 or "OK" not in proc.stdout:
        fails.append(
            "package-only import path must deliver a clean body untouched: "
            + (proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.stdout))

    # Fail-closed when the grammar truly cannot be imported: poisoning the
    # module entry makes both import forms raise, and the guard must withhold.
    poisoned = sys.modules.pop("result_markers", None)
    sys.modules["result_markers"] = None
    try:
        try:
            guard.scan_team_result("clean body", REPO, secret_filter=lambda b: _Scan(False))
            fails.append("grammar-unavailable must raise TeamResultLeakError, not deliver")
        except guard.TeamResultLeakError as exc:
            if "marker grammar unavailable" not in str(exc):
                fails.append(f"grammar-unavailable raised the wrong reason: {exc}")
        except Exception as exc:
            fails.append(f"grammar-unavailable must fail CLOSED via TeamResultLeakError, got {type(exc).__name__}")
    finally:
        if poisoned is not None:
            sys.modules["result_markers"] = poisoned
        else:
            sys.modules.pop("result_markers", None)
    return fails


def main() -> int:
    for path in (BRIDGE, WORKER):
        if not path.exists():
            print(f"FAIL: missing {path}")
            return 1
    fails = behavioral() + structural() + packaged()
    if fails:
        print("FAIL: team result guard has issues:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: non-owner results are scanned before any marker is interpreted.")
    print("  [behavioral] owner passes through; redirect/attach/no-send/secret withheld;")
    print("               unknown tier guarded; a raising scanner fails CLOSED")
    print("  [structural] guard precedes parse_markers in the delivery loop, unknown tier")
    print("               re-derived from the task file, policy defined in exactly one place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
