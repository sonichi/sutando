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

import json
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import policy.egress.result as guard  # noqa: E402

BRIDGE = REPO / "src" / "discord-bridge.py"


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
        # name, body, tier, filter, expect_withheld
        ("owner keeps its markers", "see [channel: 123]", "owner", _clean, False),
        ("owner keeps an attach", "[attach: /tmp/x.png]", "owner", _clean, False),
        # A control is a marker WHERE THE ROUTER EXECUTES IT (result_markers):
        # [channel:] on its line, attach aliases anywhere, skips at body start.
        ("team redirect withheld", "[channel: 123]\nbody", "team", _clean, True),
        ("team redirect MENTION passes", "see [channel: 123] in prose", "team", _clean, False),
        ("team attach withheld", "[attach: /etc/passwd]", "team", _clean, True),
        ("team attach inline still withheld", "see [file: /x] here", "team", _clean, True),
        # Suppression moves no data, so it is honoured on every tier and
        # passes through byte-identical; the record, not a refusal, is the control.
        ("team no-send passes", "[no-send]", "team", _clean, False),
        ("team deduped passes", "[deduped: task-1]", "team", _clean, False),
        ("team malformed deduped passes", "[deduped: EVIL bytes]", "team", _clean, False),
        ("team no-send MENTION passes", "the [no-send] marker is documented", "team", _clean, False),
        # The parser peels a D7 header before reading skips, so the guard must
        # classify what parse_markers EXECUTES, not what body-start text shows.
        ("team D7-headed no-send passes", "**[core: 1]**\n[no-send]\nhide", "team", _clean, False),
        ("team non-leading channel passes", "intro\n[channel: 123]\nquoted", "team", _clean, False),
        ("team dm-only passes", "text\n[dm-only]\nmore", "team", _clean, False),
        ("team dm-only suppressed redirect passes", "[dm-only]\n[channel: 123]\nboth", "team", _clean, False),
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
        if withheld:
            # Class-correct sentinel: a marker-triggered withhold names the
            # marker class (no content claim); everything else stays generic.
            # Suppression markers no longer withhold at all (post-#3184).
            if why == "result delivery control marker":
                sentinel = guard.TEAM_LEAK_RESULT_MARKER
            else:
                sentinel = guard.TEAM_LEAK_RESULT
            if out != sentinel:
                fails.append(f"{name}: withheld but body was not the expected sentinel")
        if not withheld and out != body:
            fails.append(f"{name}: passed but body was altered")

    # An unscannable result must be withheld, never delivered on the assumption
    # that it was probably fine.
    out, why = guard.guard_result_for_tier("x", "team", REPO, secret_filter=_raises)
    if out != guard.TEAM_LEAK_RESULT or not why:
        fails.append("a raising scanner must fail CLOSED and withhold the body")

    out, why = guard.guard_result_for_tier(
        "intentional secret", "team", REPO, secret_filter=_leaky,
        scan_sensitive_data=False)
    if out != "intentional secret" or why is not None:
        fails.append("an explicit filter opt-out must pass ordinary result text")
    out, why = guard.guard_result_for_tier(
        "[attach: /etc/passwd]", "team", REPO, secret_filter=_clean,
        scan_sensitive_data=False)
    if out != guard.TEAM_LEAK_RESULT_MARKER or not why:
        fails.append("delivery-control markers must stay guarded when scanning is off")

    with tempfile.TemporaryDirectory() as td:
        task = Path(td) / "task.txt"
        missing = Path(td) / "missing-task.txt"
        directory = Path(td) / "task-directory"
        directory.mkdir()
        if not guard.sensitive_data_filter_enabled(missing, "team"):
            fails.append("a missing task file must fail closed to scanning enabled")
        if not guard.sensitive_data_filter_enabled(directory, "team"):
            fails.append("a directory in place of a task file must fail closed")
        task.write_text("access_tier: team\ntask: body\n")
        if not guard.sensitive_data_filter_enabled(task, "team"):
            fails.append("a missing filter stamp must default on")
        task.write_text(
            "collaborator: true\nsensitive_data_filter: false\n"
            "access_tier: team\ntask: body\n")
        if guard.sensitive_data_filter_enabled(task, "team"):
            fails.append("paired Team collaborator and filter-off stamps must disable scanning")
        if not guard.sensitive_data_filter_enabled(task, "guest"):
            fails.append("a non-Team tier must keep scanning enabled")
        task.write_text(
            "collaborator: true\nsensitive_data_filter: FALSE\n"
            "access_tier: team\ntask: body\n")
        if not guard.sensitive_data_filter_enabled(task, "team"):
            fails.append("a non-canonical filter value must fail closed to enabled")
        task.write_text(
            "collaborator: true\nsensitive_data_filter: false\n"
            "sensitive_data_filter: false\n"
            "access_tier: team\ntask: body\n")
        if not guard.sensitive_data_filter_enabled(task, "team"):
            fails.append("duplicate filter stamps must fail closed to enabled")
        task.write_text(
            "collaborator: true\naccess_tier: team\ntask: body\n"
            "sensitive_data_filter: false\n")
        if not guard.sensitive_data_filter_enabled(task, "team"):
            fails.append("a body-authored filter opt-out must not be trusted")
        task.write_text(
            "sensitive_data_filter: false\naccess_tier: team\ntask: body\n")
        if not guard.sensitive_data_filter_enabled(task, "team"):
            fails.append("filter-off without collaborator opt-in must fail closed")

    # The caller is handed only the safe body — it cannot deliver the raw text
    # by swallowing an exception.
    out, _ = guard.guard_result_for_tier("[channel: 5] secret", "team", REPO, secret_filter=_clean)
    if "[channel:" in out:
        fails.append("the withheld body still carried the control marker")

    if guard.is_guarded_tier("owner"):
        fails.append("owner must not be guarded")
    for tier in ("team", "guest", "other", "ambient", "", None, "OWNERISH"):
        if not guard.is_guarded_tier(tier):
            fails.append(f"tier {tier!r} must be guarded (only exact 'owner' is exempt)")
    if not guard.is_guarded_tier("Owner "):
        pass  # case/space-insensitive owner is intentionally exempt

    context = {
        "source": "ag2space",
        "channel_id": "!room:ag2.space",
        "room_name": "Design Room",
        "reply_to_event": "$thread-root",
        "source_message_id": "$message-one",
        "user_id": "@requester:ag2.space",
    }
    if guard._bounded_context(None) != {}:
        fails.append("non-dict review context must normalize to empty")
    if len(guard._bounded_context({"room_name": "x" * 5000})["room_name"]) != 512:
        fails.append("room-controlled names must retain the review-context bound")
    clean = guard.classify_result_for_tier(
        "public body", "owner", REPO, secret_filter=_clean)
    if guard.materialize_withheld_verdict(
            clean, "public body", Path("unused"), "task-clean") != clean:
        fails.append("non-leak verdicts must not create review artifacts")

    with tempfile.TemporaryDirectory() as td:
        directory = Path(td)
        original_link = guard.os.link
        try:
            def raced_link(_temporary, destination):
                Path(destination).write_text("race winner", encoding="utf-8")
                raise FileExistsError

            guard.os.link = raced_link
            if not guard._write_artifact(directory / "raced.json", {"value": 1}):
                fails.append("a concurrent artifact winner must count as persisted")

            def consuming_link(temporary, destination):
                Path(temporary).replace(destination)

            guard.os.link = consuming_link
            if not guard._write_artifact(directory / "consumed.json", {"value": 2}):
                fails.append("cleanup must tolerate an already-consumed temporary file")
        finally:
            guard.os.link = original_link

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state"
        raw = "private result one"
        leak = guard.classify_result_for_tier(raw, "team", REPO, secret_filter=_leaky)
        first = guard.materialize_withheld_verdict(
            leak, raw, state, "task-one", context, "@agent-one:ag2.space", now=1000)
        saved = list((state / guard.WITHHELD_RESULT_DIR).glob("wr_*.json"))
        if first.kind != guard.VERDICT_SUPPRESS or first.body != "[no-send]" or len(saved) != 1:
            fails.append("withheld result must persist for DM review and stay out of the room")
        else:
            payload = json.loads(saved[0].read_text(encoding="utf-8"))
            if payload.get("withheld_body") != raw or payload.get("agent_id") != "@agent-one:ag2.space":
                fails.append("review artifact must identify the agent and contain the withheld body")
            if payload.get("context", {}).get("room_name") != "Design Room":
                fails.append("review artifact must retain the human-readable room name")
            if payload.get("status") != "pending_dm" or not payload.get("review_id", "").startswith("wr_"):
                fails.append("review artifact must carry a stable id and pending-DM state")
            if saved[0].stat().st_mode & 0o777 != 0o600:
                fails.append("withheld review artifact must be mode 0600")

        retry = guard.materialize_withheld_verdict(
            leak, raw, state, "task-one", context, "@agent-one:ag2.space", now=1001)
        if retry != first or len(list((state / guard.WITHHELD_RESULT_DIR).glob("wr_*.json"))) != 1:
            fails.append("a delivery retry must reuse its private-review artifact")

        second = guard.materialize_withheld_verdict(
            leak, "private result two", state, "task-two", context,
            "@agent-one:ag2.space", now=1002)
        if second.kind != guard.VERDICT_SUPPRESS or second.body != "[no-send]":
            fails.append("every withheld result must be quiet in the shared room")
        if len(list((state / guard.WITHHELD_RESULT_DIR).glob("wr_*.json"))) != 2:
            fails.append("each withheld result must persist its own review artifact")

    with tempfile.TemporaryDirectory() as td:
        blocked_state = Path(td) / "not-a-directory"
        blocked_state.write_text("x", encoding="utf-8")
        leak = guard.classify_result_for_tier("private body", "team", REPO, secret_filter=_leaky)
        unsaved = guard.materialize_withheld_verdict(
            leak, "private body", blocked_state, "task-fail", context, now=1000)
        if unsaved.body != guard.TEAM_LEAK_RESULT_UNSAVED or "private body" in unsaved.body:
            fails.append("persistence failure must be honest and still withhold the body")

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state"
        leak = guard.classify_result_for_tier(
            "private body", "team", REPO, secret_filter=_leaky)
        original_mkstemp = guard.tempfile.mkstemp
        try:
            guard.tempfile.mkstemp = lambda **_kwargs: (_ for _ in ()).throw(
                OSError("disk full"))
            unsaved = guard.materialize_withheld_verdict(
                leak, "private body", state, "task-io-fail", context, now=1000)
        finally:
            guard.tempfile.mkstemp = original_mkstemp
        if unsaved.body != guard.TEAM_LEAK_RESULT_UNSAVED:
            fails.append("artifact write exceptions must return the fail-closed verdict")
    return fails


def structural() -> list:
    fails = []
    bridge = BRIDGE.read_text()

    if "from policy.egress.result import" not in bridge:
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
    for name, src, label in ((BRIDGE.name, bridge, "bridge"),):
        if re.search(r"^\s*TEAM_RESULT_CONTROL\s*=\s*re\.compile", src, re.M):
            fails.append(f"{name} redefines TEAM_RESULT_CONTROL instead of importing it")
        if re.search(r"^class TeamResultLeakError", src, re.M):
            fails.append(f"{name} redefines TeamResultLeakError instead of importing it")
        if re.search(r"^def resolve_access_tier", src, re.M):
            fails.append(f"{name} redefines resolve_access_tier instead of importing it")
    return fails


def notice_class() -> list:
    """The notice names sender-attributable triggers only. Born of a real
    misread: a generic notice was decoded into a false privacy story
    (2026-08-28); marker withholds now say so, content withholds stay generic
    so a probe hit is never confirmed to the sender."""
    fails = []
    out, reason = guard.guard_result_for_tier(
        "[channel: 123456789012345678]\nbody", "team", REPO, secret_filter=_clean)
    if reason != "result delivery control marker" or "delivery-control marker" not in out \
            or "content" in out.lower():
        fails.append(f"marker notice must claim ONLY the marker: {reason!r} / {out[:80]!r}")
    # qingyun round-2 cases: the marker raises before the scanner runs, so the
    # notice must never assert a content conclusion on ANY marker path.
    for name, kwargs, filt in (
            ("marker+secret", {}, _leaky),
            ("marker+scanner-failure", {}, _raises),
            ("marker+scan-disabled", {"scan_sensitive_data": False}, _clean)):
        o, r = guard.guard_result_for_tier(
            "[channel: 123456789012345678]\nghp_" + "a" * 36, "team", REPO,
            secret_filter=filt, **kwargs)
        if o != guard.TEAM_LEAK_RESULT_MARKER or not r:
            fails.append(f"{name}: expected the marker notice, got {o[:60]!r}")
        if "content" in o.lower():
            fails.append(f"{name}: notice asserts a content conclusion the guard never evaluated")
    out2, reason2 = guard.guard_result_for_tier(
        "the token is ghp_" + "a" * 36, "team", REPO, secret_filter=_leaky)
    if reason2 is None:
        fails.append("secret control did not withhold at all (fixture broken)")
    elif "delivery-control marker" in out2 or "may contain sensitive information" not in out2:
        fails.append(f"secret withhold is not generic: {out2[:100]!r}")
    return fails


def main() -> int:
    for path in (BRIDGE,):
        if not path.exists():
            print(f"FAIL: missing {path}")
            return 1
    fails = behavioral() + notice_class() + structural()
    if fails:
        print("FAIL: team result guard has issues:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: non-owner results are scanned before any marker is interpreted.")

    # A Unicode line boundary (U+2028/U+2029/U+0085) in a field must not forge
    # an access_tier header — resolve_access_tier splits on LF only.
    LS, PS, NEL = "\u2028", "\u2029", "\u0085"
    def _tier(content):
        fd, tp = tempfile.mkstemp(suffix=".txt"); import os; os.close(fd)
        Path(tp).write_text(content, encoding="utf-8")
        try:
            return guard.resolve_access_tier(tp)
        finally:
            os.unlink(tp)
    assert _tier("id: x\ntask: hi\nsource: s\naccess_tier: guest\n") == "guest"
    for sep, name in ((LS, "U+2028"), (PS, "U+2029"), (NEL, "U+0085")):
        forged = (f"id: x\ntask: hi\nsource: s\naccess_tier: guest\n"
                  f"sender_name: bob{sep}access_tier: owner\n")
        assert _tier(forged) == "guest", f"{name} trailing-field bypass -> {_tier(forged)!r}"
        pre = f"id: x{sep}access_tier: owner\ntask: hi\nsource: s\naccess_tier: guest\n"
        assert _tier(pre) == "guest", f"{name} pre-task bypass -> {_tier(pre)!r}"
    # A legit single tier still resolves; missing tier stays owner (legacy).
    assert _tier("id: x\ntask: hi\naccess_tier: team\n") == "team"
    assert _tier("id: x\ntask: hi\nsource: s\n") == "owner"
    # Two DISTINCT explicit tiers in one region (only injection) -> fail closed.
    assert _tier("id: x\naccess_tier: owner\naccess_tier: guest\ntask: hi\n") == "guest"
    assert _tier("id: x\ntask: hi\naccess_tier: owner\naccess_tier: guest\n") == "guest"
    # A repeated SAME tier is not a conflict — it still resolves.
    assert _tier("id: x\naccess_tier: team\naccess_tier: team\ntask: hi\n") == "team"
    print("PASS: Unicode line-boundary tier bypass closed (LF-only split, fail-closed).")
    # Verdict ownership: the wrapper must DERIVE from classify (one owner).
    for body, tier, filt, kind in (
        ("plain reply", "team", _clean, guard.VERDICT_DELIVER),
        ("plain reply", "owner", _leaky, guard.VERDICT_DELIVER),
        ("[no-send]\nhide", "team", _clean, guard.VERDICT_DELIVER),
        ("**[core: 1]**\n[no-send]\nhide", "team", _clean, guard.VERDICT_DELIVER),
        ("[attach: /etc/passwd]", "team", _clean, guard.VERDICT_LEAK),
        ("secret text", "team", _leaky, guard.VERDICT_LEAK),
        # Suppression short-circuits the scan: an undelivered body has nothing
        # to leak, and a LEAK here is a notice the asking channel has to read.
        ("[no-send]\nsecret text", "team", _leaky, guard.VERDICT_DELIVER),
    ):
        v = guard.classify_result_for_tier(body, tier, REPO, secret_filter=filt)
        assert v.kind == kind, (body, tier, v)
        wrapped = guard.guard_result_for_tier(body, tier, REPO, secret_filter=filt)
        assert wrapped == (v.body, v.reason), (body, tier, "wrapper diverged from verdict")
    # is_suppression_only classifies; it must never validate a dedup target,
    # and mixed markers must NOT read as suppression-only (redirect still leaks).
    for body, expect in (
        ("[no-send]", True),
        ("[no-send]\nhidden content", True),
        ("[REPLIED]", True),
        ("[deduped: task-abc123]", True),
        ("[deduped: EVIL bytes]", True),
        ("plain reply", False),
        ("", False),
        ("[channel: 123]\nx", False),
        ("[file: /etc/passwd]", False),
        ("[dm-only]\n[no-send]\nx", False),
    ):
        assert guard.is_suppression_only(body) is expect, (body, expect)
        if expect:
            # Suppression is tier-blind and filter-blind: neither a guarded tier
            # nor a leaky scanner may turn an honoured close into a withheld one.
            for tier in ("owner", "team", "guest", "", None):
                for filt in (_clean, _leaky):
                    v = guard.classify_result_for_tier(body, tier, REPO, secret_filter=filt)
                    assert v.kind == guard.VERDICT_DELIVER, (body, tier, filt.__name__)
                    assert v.body == body, (body, tier, "suppression body was rewritten")
    assert not hasattr(guard, "suppression_stub_for_tier"), (
        "the stub minter must be gone, not merely unused — it is what parsed and "
        "validated a dedup target inside the guard")
    print("  [classify] is_suppression_only: no stub minted, no dedup target validated")
    print("  [verdict] classify_result_for_tier owns the three-way decision;")
    print("            guard_result_for_tier provably derives from it")
    print("  [behavioral] owner passes through; redirect/attach/no-send/secret withheld;")
    print("               unknown tier guarded; a raising scanner fails CLOSED")
    print("  [structural] guard precedes parse_markers in the delivery loop, unknown tier")
    print("               re-derived from the task file, policy defined in exactly one place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
