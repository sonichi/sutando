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
import os
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


# ---------------------------------------------------------------------------
# File-based helper coverage — real temp dirs, no injected fakes. These exercise
# the default implementations of the collaborators the invariant tests inject.
# ---------------------------------------------------------------------------

def _write_task(tasks_dir: Path, task_id, body, access_tier="owner", mtime=None):
    """Write a real task file `<id>.txt` with the bridge header shape."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    p = tasks_dir / f"{task_id}.txt"
    p.write_text(
        f"id: {task_id}\n"
        f"access_tier: {access_tier}\n"
        f"source: discord\n"
        f"task: {body}\n"
    )
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def case_q_recent_owner_messages_scan() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        tasks = base / "tasks"
        processed = tasks / "processed"
        results = base / "results"
        results.mkdir(parents=True, exist_ok=True)
        now = 2_000_000.0
        # owner, in-window, unresolved
        _write_task(tasks, "task-1", "login PR still broken", "owner", mtime=now - 100)
        # owner, in-window, resolved (has a result file)
        _write_task(tasks, "task-2", "deploy failing", "owner", mtime=now - 50)
        (results / "task-2.txt").write_text("done")
        # non-owner — must be filtered out
        _write_task(tasks, "task-3", "some team ask", "team", mtime=now - 10)
        # owner but OUTSIDE the window — filtered by mtime cutoff
        _write_task(tasks, "task-old", "ancient ask", "owner", mtime=now - 99_999)
        # archived under processed/ — must still be seen
        _write_task(processed, "task-4", "archived owner ask", "owner", mtime=now - 20)
        # owner, in-window, but EMPTY task body → skipped (no meaningful text)
        _write_task(tasks, "task-empty", "", "owner", mtime=now - 5)

        msgs = me._recent_owner_messages(now, 3600, tasks_dir=tasks, results_dir=results)
        texts = [m["text"] for m in msgs]
        if "some team ask" in texts:
            fails.append("q) non-owner task should be filtered out")
        if "ancient ask" in texts:
            fails.append("q) out-of-window task should be filtered by mtime cutoff")
        if "login PR still broken" not in texts:
            fails.append("q) in-window owner task should be included")
        if "archived owner ask" not in texts:
            fails.append("q) tasks/processed/ archived task should be included")
        # resolved flag
        by_text = {m["text"]: m for m in msgs}
        if not by_text.get("deploy failing", {}).get("resolved"):
            fails.append("q) task with a result file should be resolved=True")
        if by_text.get("login PR still broken", {}).get("resolved"):
            fails.append("q) task with no result file should be resolved=False")
        # newest-first order
        ts_seq = [m["ts"] for m in msgs]
        if ts_seq != sorted(ts_seq, reverse=True):
            fails.append(f"q) messages should be newest-first, got {ts_seq}")
    return fails


def case_r_parse_task_fields_multiline() -> list[str]:
    fails = []
    raw = (
        "id: task-9\n"
        "access_tier: owner\n"
        "task: first line of the body\n"
        "second continuation line\n"
        "third continuation line\n"
        "bogus_key: not a known header, part of body\n"
        "priority: normal\n"
    )
    fields = me._parse_task_fields(raw)
    if fields.get("id") != "task-9":
        fails.append(f"r) id should parse, got {fields.get('id')}")
    if fields.get("access_tier") != "owner":
        fails.append(f"r) access_tier should parse, got {fields.get('access_tier')}")
    if fields.get("priority") != "normal":
        fails.append(f"r) known header after body should parse, got {fields.get('priority')}")
    body = fields.get("task", "")
    for expect in ("first line of the body", "second continuation line",
                   "third continuation line", "bogus_key: not a known header"):
        if expect not in body:
            fails.append(f"r) multi-line task body should retain {expect!r}, got {body!r}")
    return fails


def case_s_result_exists_shapes() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td)
        (results / "task-100.txt").write_text("x")   # task- prefixed shape
        (results / "200.txt").write_text("x")         # bare-id shape
        if not me._result_exists("task-100", results):
            fails.append("s) task-<id>.txt should be found")
        if not me._result_exists("200", results):
            fails.append("s) bare-id <id>.txt should be found")
        if not me._result_exists("100", results):  # bare id maps to task-100.txt
            fails.append("s) bare id should also match task-<id>.txt")
        if me._result_exists("999", results):
            fails.append("s) absent id should not be found")
        if me._result_exists("", results):
            fails.append("s) empty id should be False")
    return fails


def case_t_detect_stuck_empty_and_no_tokens() -> list[str]:
    fails = []
    if me.detect_stuck(1_000_000, messages_fn=lambda: []) is not None:
        fails.append("t) empty messages should be None")
    # A candidate that is all stopwords / short tokens → no meaningful tokens.
    only_stop = [_msg("the it is a", 1_000_000)]
    if me.detect_stuck(1_000_100, messages_fn=lambda: only_stop) is not None:
        fails.append("t) candidate with no meaningful tokens should be None")
    return fails


def case_u_detect_resolved_paths() -> list[str]:
    fails = []
    esc_at = 1_000_000.0
    # Explicit cue after escalation → resolved (explicit).
    expl = [_msg("login 好了 切回来", 1_000_500)]
    r = me.detect_resolved(1_000_600, "login-redirect", esc_at, messages_fn=lambda: expl)
    if not r or r.get("reason") != "explicit":
        fails.append(f"u) explicit cue should resolve, got {r}")
    # Quiet window: only an on-topic frustration BEFORE the quiet window, now past it.
    quiet_msgs = [_msg("login redirect 还是不行", esc_at + 10)]
    r2 = me.detect_resolved(esc_at + 10 + me.RESOLVE_QUIET_SEC + 1, "login-redirect",
                            esc_at, messages_fn=lambda: quiet_msgs)
    if not r2 or r2.get("reason") != "quiet":
        fails.append(f"u) quiet window past frustration should resolve, got {r2}")
    # Not resolved: a recent on-topic frustration keeps the window open.
    recent = [_msg("login redirect 还是不行", 1_000_900)]
    r3 = me.detect_resolved(1_000_950, "login-redirect", esc_at, messages_fn=lambda: recent)
    if r3 is not None:
        fails.append(f"u) recent on-topic frustration should NOT resolve, got {r3}")
    # Empty topic → _on_topic returns True for all; a frustration message inside
    # the loop keeps the window open, exercising the empty-topic _on_topic branch.
    empty_topic = [_msg("还是不行 something", esc_at + 5)]
    r4 = me.detect_resolved(esc_at + 50, "", esc_at, messages_fn=lambda: empty_topic)
    if r4 is not None:
        fails.append(f"u) empty topic recent frustration should NOT resolve, got {r4}")
    # Empty topic, no frustration, past quiet window → resolves quiet.
    r5 = me.detect_resolved(esc_at + me.RESOLVE_QUIET_SEC + 1, "", esc_at,
                            messages_fn=lambda: [_msg("calm note", esc_at + 5)])
    if not r5 or r5.get("reason") != "quiet":
        fails.append(f"u) empty topic + no frustration should quiet-resolve, got {r5}")
    # Messages BEFORE escalated_at are skipped in both loops (the `continue`s).
    pre = [
        _msg("好了 切回来", esc_at - 100),               # explicit cue but pre-escalation → skipped
        _msg("login redirect 还是不行", esc_at - 50),     # frustration but pre-escalation → skipped
    ]
    r6 = me.detect_resolved(esc_at + me.RESOLVE_QUIET_SEC + 1, "login-redirect",
                            esc_at, messages_fn=lambda: pre)
    if not r6 or r6.get("reason") != "quiet":
        fails.append(f"u) pre-escalation messages should be skipped, quiet resolves, got {r6}")
    return fails


def case_v_read_state() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "nope.json"
        if me._read_state(missing) != {}:
            fails.append("v) missing state file should be {}")
        bad = Path(td) / "bad.json"
        bad.write_text("{not valid json")
        if me._read_state(bad) != {}:
            fails.append("v) malformed json should be {}")
        notdict = Path(td) / "list.json"
        notdict.write_text("[1, 2, 3]")
        if me._read_state(notdict) != {}:
            fails.append("v) non-dict json should be {}")
        good = Path(td) / "good.json"
        good.write_text(json.dumps({"escalated": True}))
        if me._read_state(good) != {"escalated": True}:
            fails.append("v) valid dict should be returned as-is")
    return fails


def case_w_act_unknown_action_and_defaults() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        # Point the module default WORKSPACE_DIR at a temp dir so the None
        # state_file / None now defaults are exercised without touching the
        # real workspace or firing a real restart/DM.
        saved_ws = me.WORKSPACE_DIR
        me.WORKSPACE_DIR = Path(td)
        try:
            calls = {"restart": [], "sent": []}
            restart = lambda mv: (calls["restart"].append(mv), True)[1]
            sender = lambda t: (calls["sent"].append(t), True)[1]
            # Unknown action, default now + state_file → falls through to None.
            r = me._act(action="noop", state_file=None, now=None,
                        restart_fn=restart, sender=sender)
            if r is not None:
                fails.append(f"w) unknown action should return None, got {r}")
            if calls["restart"] or calls["sent"]:
                fails.append("w) unknown action must not restart or DM")
            # restore when not escalated → not_escalated (default state_file path).
            r2 = me.restore_prior_model(state_file=None, now=None,
                                        restart_fn=restart, sender=sender)
            if not r2 or r2.get("action") != "not_escalated":
                fails.append(f"w) restore with no state should be not_escalated, got {r2}")
        finally:
            me.WORKSPACE_DIR = saved_ws
    return fails


def case_x_restore_restart_failure() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        h = Harness(sf)
        h.escalate(now=1_000_000, env_prior="opus[1m]")
        h.restart_ok = False
        h.restart_calls.clear()
        r = h.restore(now=1_000_500)
        if not r or r.get("action") != "restart_failed" or r.get("mode") != "restore":
            fails.append(f"x) failed restore restart should report restart_failed/restore, got {r}")
        st = json.loads(sf.read_text())
        if not st.get("escalated"):
            fails.append("x) failed restore must leave state escalated")
    return fails


def case_y_locked_state_malformed_and_lock() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        # Malformed + non-dict existing state → _locked_state falls back to {}.
        sf.write_text("[not, a, dict")
        h = Harness(sf)
        r = h.escalate(now=1_000_000, env_prior="opus[1m]")
        if not r or r.get("action") != "escalated":
            fails.append(f"y) escalate over malformed state should still escalate, got {r}")

    # _act 'locked' branch: hold the flock while _act runs (mirrors case o for _act).
    if me.fcntl is not None:
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "esc.json"
            sf.parent.mkdir(parents=True, exist_ok=True)
            lock_path = sf.with_name(sf.name + ".lock")
            holder = open(lock_path, "w")
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                h = Harness(sf)
                r = h.escalate(now=1_000_000, env_prior="opus[1m]")
                if r != {"action": "locked"}:
                    fails.append(f"y) escalate under held lock should be 'locked', got {r}")
            finally:
                fcntl.flock(holder, fcntl.LOCK_UN)
                holder.close()
    return fails


def case_z_check_once_defaults_and_stale_ask() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        saved_ws = me.WORKSPACE_DIR
        me.WORKSPACE_DIR = Path(td)
        try:
            sent = []
            sender = lambda t: (sent.append(t), True)[1]
            # Seed a stale ask_pending; a pass with no stuck topic should clear it.
            sf = Path(td) / "state" / "model-escalation.json"
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(json.dumps({"ask_pending": True, "topic": "old"}))
            # Default now + state_file (None) → uses time.time() + WORKSPACE_DIR path.
            r = me.check_once(state_file=None, now=None,
                              stuck_fn=lambda: None, sender=sender)
            if r is not None:
                fails.append(f"z) no-stuck pass should return None, got {r}")
            st = json.loads(sf.read_text())
            if st.get("ask_pending"):
                fails.append("z) stale ask_pending should be cleared when no stuck topic")
        finally:
            me.WORKSPACE_DIR = saved_ws
    return fails


def case_ab_defensive_os_and_state_branches() -> list[str]:
    """Exercise the defensive except-OSError / except-Exception fallbacks so
    they are real coverage, not pragma'd. Uses real filesystem conditions."""
    fails = []
    # --- _recent_owner_messages: unreadable + unstat-able entries -------------
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        tasks = base / "tasks"
        results = base / "results"
        tasks.mkdir(parents=True, exist_ok=True)
        results.mkdir(parents=True, exist_ok=True)
        now = 3_000_000.0
        good = _write_task(tasks, "task-ok", "login PR still broken", "owner", mtime=now - 10)
        # A *.txt that is a DIRECTORY: p.is_file() filters it, but if it slips
        # through stat/read those raise OSError. Create a dir entry to make the
        # glob branch see a non-file (is_file() False → skipped, safe).
        (tasks / "adir.txt").mkdir()
        # A broken symlink named *.txt → p.is_file() False (skipped), and if
        # stat() is forced it raises OSError. Covers the is_file gate path.
        broken = tasks / "broken.txt"
        try:
            os.symlink(str(base / "does-not-exist"), str(broken))
        except (OSError, NotImplementedError):
            pass
        msgs = me._recent_owner_messages(now, 3600, tasks_dir=tasks, results_dir=results)
        if "login PR still broken" not in [m["text"] for m in msgs]:
            fails.append("ab) good task should survive alongside skipped bad entries")

        # Force the read_text OSError branch: monkeypatch Path.read_text to raise
        # for our good file, everything else scans normally.
        orig_read = Path.read_text
        def _boom_read(self, *a, **k):
            if self == good:
                raise OSError("boom")
            return orig_read(self, *a, **k)
        Path.read_text = _boom_read
        try:
            msgs2 = me._recent_owner_messages(now, 3600, tasks_dir=tasks, results_dir=results)
        finally:
            Path.read_text = orig_read
        if any(m["text"] == "login PR still broken" for m in msgs2):
            fails.append("ab) unreadable task should be skipped (OSError branch)")

        # Force the stat() OSError branch similarly.
        orig_stat = Path.stat
        def _boom_stat(self, *a, **k):
            if self == good:
                raise OSError("boom")
            return orig_stat(self, *a, **k)
        Path.stat = _boom_stat
        try:
            me._recent_owner_messages(now, 3600, tasks_dir=tasks, results_dir=results)
        finally:
            Path.stat = orig_stat

    # --- _result_exists: exists() raising OSError ---------------------------
    with tempfile.TemporaryDirectory() as td:
        results = Path(td)
        orig_exists = Path.exists
        def _boom_exists(self, *a, **k):
            raise OSError("boom")
        Path.exists = _boom_exists
        try:
            if me._result_exists("task-x", results):
                fails.append("ab) exists() OSError should be swallowed → False")
        finally:
            Path.exists = orig_exists

    # --- _locked_state: non-dict existing state → {} ------------------------
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "esc.json"
        sf.write_text(json.dumps([1, 2, 3]))  # valid JSON, not a dict
        h = Harness(sf)
        r = h.escalate(now=1_000_000, env_prior="opus[1m]")
        if not r or r.get("action") != "escalated":
            fails.append(f"ab) non-dict state should reset to {{}} and escalate, got {r}")

    # --- _locked_state: mkdir Exception + save Exception --------------------
    with tempfile.TemporaryDirectory() as td:
        # Make the state_file's PARENT a regular file so mkdir(parents=True)
        # raises (covers the mkdir except) and write_text later fails too.
        parent_as_file = Path(td) / "blocker"
        parent_as_file.write_text("i am a file, not a dir")
        sf = parent_as_file / "esc.json"  # parent is a file → mkdir/ write raise
        h = Harness(sf)
        # Should not raise; escalate still runs its logic and the save() silently
        # no-ops (except-Exception branch), returning the action dict.
        try:
            r = h.escalate(now=1_000_000, env_prior="opus[1m]")
        except Exception as e:
            r = None
            fails.append(f"ab) escalate must not raise on unwritable state: {e}")
        if not r or r.get("action") not in ("escalated", "locked"):
            fails.append(f"ab) unwritable-state escalate should still return an action, got {r}")
    return fails


def case_aa_main_status_and_check() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        saved_ws = me.WORKSPACE_DIR
        saved_argv = sys.argv[:]
        me.WORKSPACE_DIR = Path(td)
        try:
            # --status prints the (empty) state as JSON and returns 0.
            sys.argv = ["model_escalation.py", "--status"]
            if me.main() != 0:
                fails.append("aa) main --status should return 0")
            # --check runs a real detection pass. No tasks/ dir under the temp
            # workspace → no stuck topic → action none, no restart, no DM.
            sys.argv = ["model_escalation.py", "--check"]
            if me.main() != 0:
                fails.append("aa) main --check should return 0")
            # No args → prints usage/docstring, returns 0.
            sys.argv = ["model_escalation.py"]
            if me.main() != 0:
                fails.append("aa) main with no args should return 0")
        finally:
            sys.argv = saved_argv
            me.WORKSPACE_DIR = saved_ws
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
        ("q", case_q_recent_owner_messages_scan),
        ("r", case_r_parse_task_fields_multiline),
        ("s", case_s_result_exists_shapes),
        ("t", case_t_detect_stuck_empty_and_no_tokens),
        ("u", case_u_detect_resolved_paths),
        ("v", case_v_read_state),
        ("w", case_w_act_unknown_action_and_defaults),
        ("x", case_x_restore_restart_failure),
        ("y", case_y_locked_state_malformed_and_lock),
        ("z", case_z_check_once_defaults_and_stale_ask),
        ("ab", case_ab_defensive_os_and_state_branches),
        ("aa", case_aa_main_status_and_check),
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
