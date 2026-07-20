#!/usr/bin/env python3
"""
Tests for the "auto fable" model escalation (`src/model_escalation.py`).

Tracking issue: sonichi/sutando#2224. Mirrors the injectable-collaborator style
of tests/health-check-recover-core.test.py — every side effect (core restart,
owner DM, the recent-owner-messages scan, the resolution scan) is injected, so
no real restart or Slack call ever happens.

Cases:
  a) stuck by repeat-count           → owner ASK fired, no switch yet
  b) below threshold, no frustration → no ask, no switch
  c) frustration cue below threshold → owner ASK fired (the cue alone qualifies)
  d) ask cooldown same topic         → second pass suppressed (no re-ask)
  e) new topic during cooldown       → re-asks (cooldown is per-topic)
  f) escalate_to_fable               → restart called with the fable alias,
                                        prior model recorded, state escalated
  g) restore_prior_model             → restart called with prior model, override
                                        cleared, state de-escalated
  h) restore clears the override      → prior=None restarts with model_value=None
  i) resolution (quiet window)       → switch-back prompt fired once
  j) resolution (explicit cue)       → switch-back prompt fired
  k) escalated + not resolved        → waits, no switch-back prompt
  l) DM fails on ask                 → no ask recorded, retries next pass
  m) escalate is idempotent          → second escalate is a no-op
  n) topic overlap heuristic         → same-topic re-asks group; distinct don't
  o) concurrent invocation (locked)  → second caller no-ops with "locked"
  p) restart failure on escalate     → not marked escalated, no cooldown burned

Run: python3 tests/model-escalation.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("model_escalation", REPO / "src" / "model_escalation.py")
me = importlib.util.module_from_spec(spec)
spec.loader.exec_module(me)

# Pin thresholds so the test is independent of any SUTANDO_* env override in the
# runner's environment.
me.STUCK_REPEAT_THRESHOLD = 3
me.STUCK_WINDOW_SEC = 3600
me.ESCALATE_COOLDOWN_SEC = 1800
me.RESOLVE_QUIET_SEC = 1800
me.TOPIC_OVERLAP_MIN = 2
me.FABLE_MODEL_ALIAS = "fable"


def _msg(text, ts, resolved=False):
    return {"text": text, "ts": ts, "resolved": resolved}


class Harness:
    """Drives model_escalation entrypoints with injected, recording collaborators."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.sent: list[str] = []
        self.restart_calls: list = []
        self.restart_ok = True
        self.send_ok = True

    def sender(self, text):
        self.sent.append(text)
        return self.send_ok

    def restart(self, model_value):
        self.restart_calls.append(model_value)
        return self.restart_ok

    def check(self, now, messages=None, stuck=None, resolved=None):
        stuck_fn = None
        resolved_fn = None
        if stuck is not None:
            stuck_fn = lambda: stuck
        elif messages is not None:
            stuck_fn = lambda: me.detect_stuck(now, messages_fn=lambda: messages)
        if resolved is not None:
            resolved_fn = lambda: resolved
        elif messages is not None:
            resolved_fn = lambda: None  # default: not resolved unless provided
        return me.check_once(
            state_file=self.state_file, now=now,
            stuck_fn=stuck_fn, resolved_fn=resolved_fn, sender=self.sender,
        )

    def escalate(self, now, env_prior=None):
        import os
        saved = os.environ.get("SUTANDO_CORE_MODEL")
        if env_prior is not None:
            os.environ["SUTANDO_CORE_MODEL"] = env_prior
        else:
            os.environ.pop("SUTANDO_CORE_MODEL", None)
        try:
            return me.escalate_to_fable(
                state_file=self.state_file, now=now,
                restart_fn=self.restart, sender=self.sender,
            )
        finally:
            if saved is None:
                os.environ.pop("SUTANDO_CORE_MODEL", None)
            else:
                os.environ["SUTANDO_CORE_MODEL"] = saved

    def restore(self, now):
        return me.restore_prior_model(
            state_file=self.state_file, now=now,
            restart_fn=self.restart, sender=self.sender,
        )


def case_a_stuck_by_repeat_count() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "esc.json")
        msgs = [
            _msg("the login PR keeps redirecting wrong", 1_000_300),
            _msg("login PR still redirecting", 1_000_200),
            _msg("fix the login PR redirect", 1_000_100),
        ]
        r = h.check(now=1_000_400, messages=msgs)
        if not r or r.get("action") != "asked":
            fails.append(f"a) 3 unresolved same-topic sends should ASK, got {r}")
        if h.restart_calls:
            fails.append("a) ask should NOT switch the model")
        if len(h.sent) != 1:
            fails.append(f"a) should DM the owner exactly once, sent {len(h.sent)}")
    return fails


def case_b_below_threshold_no_action() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "esc.json")
        msgs = [
            _msg("look at the login PR redirect", 1_000_200),
            _msg("check the login PR", 1_000_100),
        ]
        r = h.check(now=1_000_300, messages=msgs)
        if r is not None:
            fails.append(f"b) 2 calm sends should not act, got {r}")
        if h.sent or h.restart_calls:
            fails.append("b) below-threshold triggered DM/restart")
    return fails


def case_c_frustration_below_threshold_asks() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "esc.json")
        msgs = [
            _msg("login PR redirect 还是不行", 1_000_200),
            _msg("look at the login PR redirect", 1_000_100),
        ]
        r = h.check(now=1_000_300, messages=msgs)
        if not r or r.get("action") != "asked":
            fails.append(f"c) frustration cue should ASK even below repeat threshold, got {r}")
        if not r or not r.get("topic"):
            fails.append("c) ask should carry a topic")
    return fails


def case_d_ask_cooldown_suppresses_reask() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "esc.json")
        msgs = [
            _msg("login PR redirect 还是不行", 1_000_300),
            _msg("login PR redirect broken", 1_000_200),
            _msg("login PR redirect", 1_000_100),
        ]
        h.check(now=1_000_400, messages=msgs)          # first ask
        r = h.check(now=1_000_500, messages=msgs)      # +100s < cooldown(1800)
        if r and r.get("action") == "asked":
            fails.append("d) re-asked within cooldown on same topic")
        if r and r.get("action") != "ask_cooldown":
            fails.append(f"d) same-topic within cooldown should be 'ask_cooldown', got {r}")
        if len(h.sent) != 1:
            fails.append(f"d) cooldown should leave a single ask DM, sent {len(h.sent)}")
    return fails


def case_e_new_topic_reasks_during_cooldown() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "esc.json")
        topic1 = [
            _msg("login PR redirect 还是不行", 1_000_300),
            _msg("login PR redirect broken", 1_000_200),
            _msg("login PR redirect", 1_000_100),
        ]
        h.check(now=1_000_400, messages=topic1)        # ask about login
        topic2 = [
            _msg("deploy pipeline 还是不行", 1_000_450),
            _msg("deploy pipeline failing", 1_000_440),
            _msg("deploy pipeline stuck", 1_000_430),
        ]
        r = h.check(now=1_000_500, messages=topic2)    # different topic, within cooldown
        if not r or r.get("action") != "asked":
            fails.append(f"e) a NEW stuck topic should re-ask even in cooldown, got {r}")
        if len(h.sent) != 2:
            fails.append(f"e) two distinct topics should produce two asks, sent {len(h.sent)}")
    return fails


def case_f_escalate_sets_env_and_restarts() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        r = h.escalate(now=1_000_000, env_prior="opus[1m]")
        if not r or r.get("action") != "escalated":
            fails.append(f"f) escalate should report 'escalated', got {r}")
        if h.restart_calls != ["fable"]:
            fails.append(f"f) escalate must restart with the fable alias, got {h.restart_calls}")
        st = json.loads(sf.read_text())
        if not st.get("escalated"):
            fails.append("f) state should be marked escalated")
        if st.get("prior_model") != "opus[1m]":
            fails.append(f"f) prior model should be recorded, got {st.get('prior_model')}")
        if not h.sent:
            fails.append("f) escalate should DM the owner")
    return fails


def case_g_restore_restarts_with_prior() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior="opus[1m]")
        h.restart_calls.clear()
        r = h.restore(now=1_000_500)
        if not r or r.get("action") != "restored":
            fails.append(f"g) restore should report 'restored', got {r}")
        if h.restart_calls != ["opus[1m]"]:
            fails.append(f"g) restore must restart with the prior model, got {h.restart_calls}")
        st = json.loads(sf.read_text())
        if st.get("escalated"):
            fails.append("g) state should be de-escalated after restore")
        if "prior_model" in st:
            fails.append("g) prior_model should be cleared after restore")
    return fails


def case_h_restore_clears_override_when_no_prior() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior=None)   # no prior override set
        h.restart_calls.clear()
        r = h.restore(now=1_000_500)
        if not r or r.get("action") != "restored":
            fails.append(f"h) restore should report 'restored', got {r}")
        if h.restart_calls != [None]:
            fails.append(f"h) restore with no prior must clear the override (None), got {h.restart_calls}")
    return fails


def case_i_resolution_quiet_prompts_switch_back() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior="opus[1m]")
        h.sent.clear()
        r = h.check(now=1_000_100, resolved={"reason": "quiet", "quiet_for": 2000})
        if not r or r.get("action") != "resolve_prompted":
            fails.append(f"i) quiet resolution should prompt switch-back, got {r}")
        if len(h.sent) != 1:
            fails.append(f"i) switch-back prompt should DM once, sent {len(h.sent)}")
        # A second pass while still pending must not re-prompt.
        r2 = h.check(now=1_000_200, resolved={"reason": "quiet", "quiet_for": 2100})
        if r2 and r2.get("action") == "resolve_prompted":
            fails.append("i) switch-back prompt not deduped")
        if len(h.sent) != 1:
            fails.append(f"i) switch-back prompt re-fired, sent {len(h.sent)}")
    return fails


def case_j_resolution_explicit_cue_prompts() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior="opus[1m]")
        h.sent.clear()
        r = h.check(now=1_000_100, resolved={"reason": "explicit", "sample": "好了 切回来"})
        if not r or r.get("action") != "resolve_prompted" or r.get("reason") != "explicit":
            fails.append(f"j) explicit cue should prompt switch-back, got {r}")
    return fails


def case_k_escalated_not_resolved_waits() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior="opus[1m]")
        h.sent.clear()
        r = h.check(now=1_000_100, resolved=None)
        if not r or r.get("action") != "escalated_waiting":
            fails.append(f"k) not-resolved should wait, got {r}")
        if h.sent:
            fails.append("k) waiting state should not DM")
    return fails


def case_l_ask_dm_fails_retries() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.send_ok = False
        msgs = [
            _msg("login PR redirect 还是不行", 1_000_300),
            _msg("login PR redirect broken", 1_000_200),
            _msg("login PR redirect", 1_000_100),
        ]
        r = h.check(now=1_000_400, messages=msgs)
        if not r or r.get("action") != "ask_failed":
            fails.append(f"l) failed ask DM should report 'ask_failed', got {r}")
        st = json.loads(sf.read_text()) if sf.exists() else {}
        if st.get("ask_pending"):
            fails.append("l) failed DM should not record the ask (must retry)")
        # Next pass with the DM working should ask.
        h.send_ok = True
        r2 = h.check(now=1_000_500, messages=msgs)
        if not r2 or r2.get("action") != "asked":
            fails.append(f"l) retry after failed ask should ask, got {r2}")
    return fails


def case_m_escalate_idempotent() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior="opus[1m]")
        h.restart_calls.clear()
        r = h.escalate(now=1_000_100, env_prior="opus[1m]")
        if not r or r.get("action") != "already_escalated":
            fails.append(f"m) second escalate should be a no-op, got {r}")
        if h.restart_calls:
            fails.append("m) idempotent escalate must not restart again")
    return fails


def case_n_topic_overlap_heuristic() -> list[str]:
    fails = []
    # Same topic groups → 3 unresolved → stuck.
    same = [
        _msg("login PR redirect wrong", 1_000_300),
        _msg("login PR redirect broken", 1_000_200),
        _msg("login redirect PR", 1_000_100),
    ]
    r = me.detect_stuck(1_000_400, messages_fn=lambda: same)
    if not r:
        fails.append("n) 3 same-topic unresolved sends should be stuck")
    # Distinct topics → candidate has < threshold same-topic → not stuck.
    distinct = [
        _msg("login PR redirect", 1_000_300),
        _msg("deploy pipeline failing", 1_000_200),
        _msg("email triage slow", 1_000_100),
    ]
    r2 = me.detect_stuck(1_000_400, messages_fn=lambda: distinct)
    if r2:
        fails.append(f"n) distinct topics should not be stuck, got {r2}")
    return fails


def case_o_lock_prevents_concurrent() -> list[str]:
    if me.fcntl is None:
        return []  # no POSIX locking on this platform
    import fcntl
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        lock_path = sf.with_name(sf.name + ".lock")
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            h = Harness(sf)
            r = h.check(now=1_000_000, stuck={"topic": "x", "repeat_count": 3, "frustrated": True})
            if r != {"action": "locked"}:
                fails.append(f"o) concurrent call should be 'locked', got {r}")
            if h.sent or h.restart_calls:
                fails.append("o) locked call acted despite held lock")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
    return fails


def case_p_restart_failure_on_escalate() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.restart_ok = False
        r = h.escalate(now=1_000_000, env_prior="opus[1m]")
        if not r or r.get("action") != "restart_failed":
            fails.append(f"p) failed restart should report 'restart_failed', got {r}")
        st = json.loads(sf.read_text()) if sf.exists() else {}
        if st.get("escalated"):
            fails.append("p) failed restart must not mark escalated")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_stuck_by_repeat_count),
        ("b", case_b_below_threshold_no_action),
        ("c", case_c_frustration_below_threshold_asks),
        ("d", case_d_ask_cooldown_suppresses_reask),
        ("e", case_e_new_topic_reasks_during_cooldown),
        ("f", case_f_escalate_sets_env_and_restarts),
        ("g", case_g_restore_restarts_with_prior),
        ("h", case_h_restore_clears_override_when_no_prior),
        ("i", case_i_resolution_quiet_prompts_switch_back),
        ("j", case_j_resolution_explicit_cue_prompts),
        ("k", case_k_escalated_not_resolved_waits),
        ("l", case_l_ask_dm_fails_retries),
        ("m", case_m_escalate_idempotent),
        ("n", case_n_topic_overlap_heuristic),
        ("o", case_o_lock_prevents_concurrent),
        ("p", case_p_restart_failure_on_escalate),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nAll model-escalation invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
