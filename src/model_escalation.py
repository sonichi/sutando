#!/usr/bin/env python3
"""
Sutando "auto fable" — escalate the core model to Fable 5 when the same
problem keeps not getting solved, then restore the prior model once it is.

Usage:
  python3 src/model_escalation.py --check    # run one pass: detect + drive the yes/no reply
  python3 src/model_escalation.py --status   # print current escalation state as JSON
  python3 src/model_escalation.py --escalate # manual override: switch to Fable 5 now
  python3 src/model_escalation.py --restore  # manual override: switch back now

Tracking issue: sonichi/sutando#2224.

The mechanism is a deliberate mirror of health-check.py's
recover_core_if_wedged (the canonical "restart-the-core while overriding
SUTANDO_CORE_MODEL" pattern):

  * All side-effecting collaborators are injectable so the detection /
    escalation / restore logic is unit-tested without real restarts or DMs.
  * The whole load -> decide -> act -> save sequence runs under an exclusive,
    non-blocking flock on `<state_file>.lock`, so a manual `--check` and a
    cron tick firing in the same window can't double-escalate.
  * A cooldown persisted in `<workspace>/state/model-escalation.json` bounds
    how often we re-ask the owner.
  * The owner is DM'd via the same Slack sender recover_core uses.

Unlike recover_core (which acts autonomously on a wedge), auto-fable is
owner-gated: stuck-detected sends the owner an ASK ("Switch to Fable 5?") and
records that the ask is pending. The actual switch happens when the owner
answers yes, and the switch-back happens when the owner answers yes to the
"switch back?" prompt.

The `--check` pass IS the state machine: it drives the yes/no automatically,
no external tool required. When an ask is pending it reads the owner's latest
message since the ask (`last_ask_at`) and, if it carries an affirmative cue
(AFFIRM_KEYWORDS), calls escalate_to_fable; a negative cue (DECLINE_KEYWORDS)
clears the ask (records a decline so it doesn't immediately re-ask); no reply
leaves the ask pending. Symmetrically, once escalated + a switch-back prompt is
pending, an affirmative reply calls restore_prior_model and a negative reply
clears the prompt (stays on Fable). escalate_to_fable / restore_prior_model are
also exposed as `--escalate` / `--restore` CLI subcommands for a manual
override / a concrete invocation point, and for tests.

How the switch is performed
---------------------------
start-cli.sh reads $SUTANDO_CORE_MODEL and passes it through verbatim as
`--model` (unset = inherit the global model). recover_core already uses this
to pin `opus` on a re-wedge. We do the analogous thing with the Fable alias:
set SUTANDO_CORE_MODEL in the env of a `src/agent/start-cli.sh --restart`
subprocess, which kills the core tmux session and relaunches it with
`--model <fable>`. The prior SUTANDO_CORE_MODEL value is recorded in the state
file so restore can put it back (or clear it to re-inherit the global model).

Fable alias
-----------
The model id is `claude-fable-5`. The Claude Code CLI `--model` flag accepts a
short family alias the same way it accepts `opus` / `sonnet` (recover_core
passes the bare `opus`), so we pass the analogous `fable` alias. The full id is
also accepted; `fable` keeps parity with the existing `opus` escalation and is
the value the model-catalog maps "fable" to. See FABLE_MODEL_ALIAS below.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX file locking for the escalation critical section
except ImportError:  # pragma: no cover — non-POSIX (e.g. Windows), lock no-ops
    fcntl = None

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE_DIR = resolve_workspace()

# ---------------------------------------------------------------------------
# Tuning constants (env-overridable, same convention as RECOVER_* in
# health-check.py). Kept together at the top because this is a first-version
# heuristic and the owner will want to tune it.
# ---------------------------------------------------------------------------

# Fable model alias passed as `--model`. See the module docstring — the CLI
# accepts a short alias (like `opus`) and the model id is `claude-fable-5`.
FABLE_MODEL_ALIAS = os.environ.get("SUTANDO_FABLE_MODEL", "fable")

# How many times the SAME topic must be re-sent by the owner (without getting
# resolved) inside the window before we call it "stuck". The first send is not
# a repeat, so N=3 means the owner asked about it 3 times.
STUCK_REPEAT_THRESHOLD = int(os.environ.get("SUTANDO_STUCK_REPEAT_THRESHOLD", "3"))

# Only owner messages within this trailing window count toward a repeat streak.
# A slow trickle of the same ask over days is not "stuck right now".
STUCK_WINDOW_SEC = int(os.environ.get("SUTANDO_STUCK_WINDOW_SEC", "3600"))

# Minimum gap between owner ASKs — one escalate-cooldown so we don't re-prompt
# the owner every cron tick while they're still deciding.
ESCALATE_COOLDOWN_SEC = int(os.environ.get("SUTANDO_ESCALATE_COOLDOWN_SEC", "1800"))

# Resolution signal: once escalated, if NO frustrated re-send about the topic
# arrives for this quiet window, treat the problem as resolved and prompt to
# switch back. An explicit owner "fixed / 好了 / switch back" cue triggers it
# immediately regardless of the window (see RESOLVED_KEYWORDS).
RESOLVE_QUIET_SEC = int(os.environ.get("SUTANDO_RESOLVE_QUIET_SEC", "1800"))

# Frustration cues. Matched case-insensitively as substrings against the owner
# message text. English + Chinese, since the owner mixes both. First-version
# list — extend freely; keep it a plain substring match so it stays cheap and
# well-understood.
FRUSTRATION_KEYWORDS = [
    # English
    "still broken", "still not working", "still failing", "still doesn't",
    "still doesn't work", "didn't fix", "not fixed", "doesn't work",
    "why isn't", "why is this not", "keeps failing", "again", "come on",
    "for the nth time", "how many times",
    # Chinese
    "还是不行", "还是不好", "还是没", "没改好", "怎么还", "为什么都没用",
    "说了这么多遍", "又不行", "还不行", "老问题", "怎么又",
]

# Explicit "the problem is resolved / switch back" cues from the owner.
RESOLVED_KEYWORDS = [
    # English
    "fixed", "it works now", "works now", "resolved", "switch back",
    "you can switch back", "back to normal",
    # Chinese
    "好了", "行了", "解决了", "可以了", "切回来", "换回来", "没问题了",
]

# Affirmative cues — an owner "yes" to a pending ASK / switch-back prompt.
# Matched case-insensitively as substrings, same as the other cue lists.
AFFIRM_KEYWORDS = [
    # English
    "yes", "yeah", "yep", "do it", "go ahead", "switch", "ok", "okay",
    "sure", "please do",
    # Chinese
    "好", "行", "可以", "切", "换", "是", "好的", "切吧",
]

# Negative cues — an owner "no" to a pending ASK / switch-back prompt.
DECLINE_KEYWORDS = [
    # English
    "no", "nope", "don't", "do not", "not now", "leave it", "stay",
    # Chinese
    "不用", "别", "不要", "算了", "先不", "不切", "不换",
]

# Tokens ignored when comparing two owner messages for "same topic" overlap —
# stopwords + the frustration/resolution cues themselves (so "PR login still
# broken" and "login PR again" match on the meaningful tokens, not on "still"
# or "again").
_TOPIC_STOPWORDS = {
    "the", "a", "an", "is", "it", "this", "that", "to", "of", "and", "or",
    "in", "on", "for", "with", "my", "me", "you", "still", "not", "again",
    "why", "isn't", "doesn't", "work", "working", "fix", "fixed", "please",
    "can", "u", "pls", "都", "了", "的", "还", "又", "没", "为什么",
    "怎么", "这", "个", "我", "你",
}

# Minimum number of shared meaningful tokens for two owner messages to count as
# "the same topic". Low on purpose — a first-version heuristic favouring recall.
TOPIC_OVERLAP_MIN = int(os.environ.get("SUTANDO_TOPIC_OVERLAP_MIN", "2"))


# ---------------------------------------------------------------------------
# Owner-message extraction — the stuck signal
# ---------------------------------------------------------------------------

def _tokenize_topic(text: str) -> "set[str]":
    """Lower-cased meaningful tokens of an owner message, stopwords removed.
    Splits on non-word runs (Unicode-aware) so CN and EN both tokenize; single
    CJK chars survive as tokens. Used for the cheap same-topic overlap test."""
    toks = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    return {t for t in toks if t and t not in _TOPIC_STOPWORDS and len(t) > 1}


def _has_keyword(text: str, keywords) -> bool:
    """Case-insensitive substring match of any keyword in the text."""
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def _recent_owner_messages(
    now: float,
    window_sec: int,
    tasks_dir: Optional[Path] = None,
    results_dir: Optional[Path] = None,
) -> "list[dict]":
    """Owner-authored task messages seen in the trailing `window_sec`, newest
    first. Each entry: {"text": str, "ts": float, "resolved": bool} where
    `resolved` is True if a result file for that task id exists (the task got
    an answer). Scans top-level tasks/*.txt plus the canonical month-partitioned
    archive tasks/archive/YYYY-MM/*.txt (the task-bridge archives drained tasks
    there — see src/health-check.py and src/task-bridge.ts) so re-asks that
    already drained are still visible.

    A task counts as owner-authored when its `access_tier:` is `owner` (the
    bridges tag every task). Non-owner tasks never signal owner frustration, so
    they're skipped. This mirrors how check_task_queue / task-bridge glob the
    same dirs — no new file convention introduced."""
    if tasks_dir is None:
        tasks_dir = WORKSPACE_DIR / "tasks"
    if results_dir is None:
        results_dir = WORKSPACE_DIR / "results"

    # Scan the top-level queue plus each month-partitioned archive subdir
    # (tasks/archive/YYYY-MM/). Glob the archive month dirs rather than rebuild
    # YYYY-MM from now, since a task's archive month can differ from the current
    # one around month boundaries (same reasoning as task-bridge._isVoiceTask).
    scan_dirs = [tasks_dir]
    archive_root = tasks_dir / "archive"
    try:
        scan_dirs.extend(d for d in archive_root.iterdir() if d.is_dir())
    except OSError:
        pass

    files: "list[Path]" = []
    for base in scan_dirs:
        try:
            files.extend(p for p in base.glob("*.txt") if p.is_file())
        except OSError:
            continue

    cutoff = now - window_sec
    out: "list[dict]" = []
    for p in files:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            continue
        fields = _parse_task_fields(raw)
        if fields.get("access_tier", "owner") != "owner":
            continue
        text = fields.get("task", "").strip()
        if not text:
            continue
        task_id = fields.get("id") or p.stem
        resolved = _result_exists(task_id, results_dir)
        out.append({"text": text, "ts": mtime, "resolved": resolved})

    out.sort(key=lambda m: m["ts"], reverse=True)
    return out


def _parse_task_fields(raw: str) -> "dict":
    """Parse the `key: value` header lines of a task file into a dict. The
    `task:` value is everything from the `task:` line up to the next known
    header key (task bodies span multiple lines — see the discord bridge).
    Only the header keys we care about are recognized; unknown lines inside the
    task body are kept as part of `task`."""
    known = (
        "id", "timestamp", "task", "source", "channel_id", "channel_name",
        "guild_name", "source_message_id", "chat_id", "user_id",
        "access_tier", "priority", "interaction_type",
    )
    fields: "dict" = {}
    cur_key = None
    for line in raw.splitlines():
        m = re.match(r"^([a-z_]+):\s?(.*)$", line)
        if m and m.group(1) in known:
            cur_key = m.group(1)
            fields[cur_key] = m.group(2)
        elif cur_key == "task":
            # Continuation of a multi-line task body.
            fields["task"] = fields.get("task", "") + "\n" + line
    return fields


def _result_exists(task_id: str, results_dir: Path) -> bool:
    """True if a result file for this task id exists (task got an answer).
    Checks the default `results/task-<id>.txt` shape and the bare id, matching
    the task-bridge / bridge result-file naming."""
    if not task_id:
        return False
    if task_id.startswith("task-"):
        candidates = [results_dir / f"{task_id}.txt"]
    else:
        candidates = [
            results_dir / f"{task_id}.txt",
            results_dir / f"task-{task_id}.txt",
        ]
    for c in candidates:
        try:
            if c.exists():
                return True
        except OSError:
            continue
    return False


def detect_stuck(
    now: float,
    messages_fn=None,
) -> "dict | None":
    """Decide whether the owner is stuck on a topic. Returns a dict describing
    the stuck topic, or None. Injectable `messages_fn()` returns the list of
    recent owner messages (see _recent_owner_messages) so the heuristic is
    unit-tested without files.

    Heuristic (first version, deliberately simple):
      1. Take the newest owner message as the candidate topic.
      2. Count how many of the recent owner messages share the topic (>=
         TOPIC_OVERLAP_MIN meaningful tokens with the candidate) AND are NOT
         resolved (no result file). That's the unresolved repeat count.
      3. Stuck when EITHER the unresolved repeat count reaches
         STUCK_REPEAT_THRESHOLD, OR any of the topic messages carries a
         frustration cue (the owner said it out loud).

    The topic label returned is a short slug of the shared tokens, used for the
    owner DM and to key the state so a NEW topic can escalate later."""
    messages_fn = messages_fn or (
        lambda: _recent_owner_messages(now, STUCK_WINDOW_SEC)
    )
    messages = messages_fn()
    if not messages:
        return None

    candidate = messages[0]
    cand_tokens = _tokenize_topic(candidate["text"])
    if not cand_tokens:
        return None

    same_topic = []
    for m in messages:
        overlap = cand_tokens & _tokenize_topic(m["text"])
        if len(overlap) >= TOPIC_OVERLAP_MIN or m is candidate:
            same_topic.append(m)

    unresolved = [m for m in same_topic if not m["resolved"]]
    frustrated = any(_has_keyword(m["text"], FRUSTRATION_KEYWORDS) for m in same_topic)

    repeat_count = len(unresolved)
    if repeat_count < STUCK_REPEAT_THRESHOLD and not frustrated:
        return None

    topic = _topic_slug(cand_tokens)
    return {
        "topic": topic,
        "repeat_count": repeat_count,
        "frustrated": frustrated,
        "sample": candidate["text"][:200],
        "last_ts": candidate["ts"],
    }


def detect_resolved(
    now: float,
    topic: str,
    escalated_at: float,
    messages_fn=None,
) -> "dict | None":
    """Decide whether an escalated topic now looks resolved. Returns a dict
    describing the resolution signal, or None. Injectable `messages_fn()` as in
    detect_stuck.

    Resolved when EITHER:
      * an owner message on the topic (or any owner message) carries an
        explicit resolution cue (`fixed` / `好了` / `switch back` / ...), OR
      * no frustrated re-send about the topic has arrived for RESOLVE_QUIET_SEC
        since the escalation (the storm passed).
    """
    messages_fn = messages_fn or (
        lambda: _recent_owner_messages(now, max(STUCK_WINDOW_SEC, RESOLVE_QUIET_SEC))
    )
    messages = messages_fn()

    topic_tokens = set(topic.split("-")) if topic else set()

    def _on_topic(text: str) -> bool:
        if not topic_tokens:
            return True
        return len(topic_tokens & _tokenize_topic(text)) >= 1

    # Explicit "fixed / switch back" cue — immediate, regardless of quiet window.
    for m in messages:
        if m["ts"] < escalated_at:
            continue
        if _has_keyword(m["text"], RESOLVED_KEYWORDS):
            return {"reason": "explicit", "sample": m["text"][:200]}

    # Quiet window: no frustrated re-send on the topic since escalation.
    last_frustration = 0.0
    for m in messages:
        if m["ts"] < escalated_at:
            continue
        if _on_topic(m["text"]) and _has_keyword(m["text"], FRUSTRATION_KEYWORDS):
            last_frustration = max(last_frustration, m["ts"])

    quiet_since = max(escalated_at, last_frustration)
    if now - quiet_since >= RESOLVE_QUIET_SEC:
        return {"reason": "quiet", "quiet_for": int(now - quiet_since)}
    return None


def owner_reply_after(
    now: float,
    since: float,
    messages_fn=None,
) -> "str | None":
    """Classify the owner's latest message strictly AFTER `since` (the ask /
    switch-back prompt timestamp) as an approval decision. Returns:
      * "yes"  — the latest post-`since` owner message carries an affirmative cue
      * "no"   — it carries a negative cue
      * None   — no owner message after `since`, or it's neither cue.

    Only the single newest post-`since` owner message decides — an old "yes" in
    the window can't retro-approve, and a fresh "no" overrides a stale "yes".
    Injectable `messages_fn()` (same shape as detect_stuck) keeps it hermetic.
    An affirmative check runs first so a message that trips both lists (rare —
    e.g. "no, switch") still reads as the more specific cue order the lists
    encode; in practice cues are disjoint."""
    messages_fn = messages_fn or (
        lambda: _recent_owner_messages(now, STUCK_WINDOW_SEC)
    )
    messages = messages_fn()
    # messages are newest-first; take the first one strictly after `since`.
    for m in messages:
        if m["ts"] <= since:
            continue
        text = m["text"]
        if _has_keyword(text, AFFIRM_KEYWORDS):
            return "yes"
        if _has_keyword(text, DECLINE_KEYWORDS):
            return "no"
        return None
    return None


def _topic_slug(tokens: "set[str]") -> str:
    """A short, stable slug of the shared topic tokens for the DM + state key.
    Sorted so the same token set always yields the same slug."""
    return "-".join(sorted(tokens))[:60] or "topic"


# ---------------------------------------------------------------------------
# The switch — mirror recover_core's restart-with-env pattern
# ---------------------------------------------------------------------------

def _resolve_launch_env(model_value: Optional[str]) -> dict:  # pragma: no cover
    """Environment for the out-of-process core restart. Prepends the same PATH
    dirs recover_core's _resolve_launch_env does (homebrew, ~/.local/bin, the
    app-bundled runtime) so `start-cli.sh --restart` finds tmux / node / claude
    under a minimal launchd/cron PATH.

    `model_value` is the SUTANDO_CORE_MODEL to inject: the fable alias to
    escalate, the prior value to restore, or None to CLEAR the override so the
    restarted core re-inherits the global model."""
    env = dict(os.environ)
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    bundled_bin = REPO_DIR.parent / "runtime" / "bin"
    if bundled_bin.is_dir():  # pragma: no cover — only inside the app bundle
        extra.append(str(bundled_bin))
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "/usr/bin:/bin")
    if model_value:
        env["SUTANDO_CORE_MODEL"] = model_value
    else:
        env.pop("SUTANDO_CORE_MODEL", None)
    return env


def _default_core_restart(model_value: Optional[str]) -> bool:  # pragma: no cover
    """Restart the core via `src/agent/start-cli.sh --restart`, injecting
    SUTANDO_CORE_MODEL=<model_value> (or clearing it when model_value is None).
    start-cli.sh reads that env at launch and passes it as `--model`. Returns
    True if the restart command exited 0. Mirrors recover_core's
    _default_core_restart."""
    script = REPO_DIR / "src" / "agent" / "start-cli.sh"
    if not script.exists():
        return False
    env = _resolve_launch_env(model_value)  # pragma: no cover — real-subprocess path
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script), "--restart"],
            env=env, capture_output=True, text=True, timeout=120,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _default_owner_dm(text: str) -> bool:  # pragma: no cover
    """DM the owner via the SAME Slack sender recover_core uses. health-check.py
    is hyphenated so it can't be a normal import; load it by path (the same
    technique tests/health-check-recover-core.test.py uses) and call its
    _default_slack_sender. Returns False (never raises) if health-check or the
    Slack creds are unavailable, so escalation never blocks on the DM."""
    try:  # pragma: no cover — exercised via injected sender in tests
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "health_check_for_dm", REPO_DIR / "src" / "health-check.py"
        )
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        return bool(hc._default_slack_sender(text))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State + the flock-guarded passes
# ---------------------------------------------------------------------------

class _locked_state:
    """Context manager: acquire the non-blocking flock on `<state_file>.lock`,
    load the JSON state, and yield (state_dict, save_callable). Yields None if
    another invocation holds the lock (caller returns {"action": "locked"}).
    Mirrors recover_core's flock + load + _save pattern, factored out because
    both the detection pass and the escalate/restore path need it."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.lock_fh = None

    def __enter__(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        lock_path = self.state_file.with_name(self.state_file.name + ".lock")
        if fcntl is not None:
            try:
                self.lock_fh = open(lock_path, "w")
                fcntl.flock(self.lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if self.lock_fh is not None:
                    self.lock_fh.close()
                    self.lock_fh = None
                return None
        try:
            state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}

        def _save():
            try:
                self.state_file.write_text(json.dumps(state))
            except Exception:
                pass

        return state, _save

    def __exit__(self, *exc):
        if self.lock_fh is not None:
            try:
                fcntl.flock(self.lock_fh, fcntl.LOCK_UN)
            except Exception:
                pass
            self.lock_fh.close()
            self.lock_fh = None
        return False


def escalate_to_fable(
    state_file: Optional[Path] = None,
    now: Optional[float] = None,
    restart_fn=None,
    sender=None,
) -> "dict | None":
    """Perform the switch to Fable 5: record the prior SUTANDO_CORE_MODEL,
    restart the core with SUTANDO_CORE_MODEL=<fable>, and DM the owner. Called
    when the owner answers YES to the stuck prompt. Idempotent — a no-op if
    already escalated. Returns an action dict or None."""
    return _act(
        action="escalate", state_file=state_file, now=now,
        restart_fn=restart_fn, sender=sender,
    )


def restore_prior_model(
    state_file: Optional[Path] = None,
    now: Optional[float] = None,
    restart_fn=None,
    sender=None,
) -> "dict | None":
    """Undo the switch: restart the core with the recorded prior
    SUTANDO_CORE_MODEL (or clear it to re-inherit the global model) and DM the
    owner. Called when the owner answers YES to the switch-back prompt. A no-op
    if not currently escalated. Returns an action dict or None."""
    return _act(
        action="restore", state_file=state_file, now=now,
        restart_fn=restart_fn, sender=sender,
    )


def _act(
    action: str,
    state_file: Optional[Path],
    now: Optional[float],
    restart_fn,
    sender,
) -> "dict | None":
    """Shared escalate/restore body under the flock. Kept separate from the
    detection pass so the yes-path (owner said yes) can be driven directly by
    the core agent without re-running detection."""
    if now is None:
        now = time.time()
    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "model-escalation.json"
    restart_fn = restart_fn or _default_core_restart
    send = sender or _default_owner_dm

    with _locked_state(state_file) as ctx:
        if ctx is None:
            return {"action": "locked"}
        state, save = ctx

        if action == "escalate":
            if state.get("escalated"):
                return {"action": "already_escalated"}
            prior = os.environ.get("SUTANDO_CORE_MODEL") or None
            if not restart_fn(FABLE_MODEL_ALIAS):
                return {"action": "restart_failed", "mode": "escalate"}
            state["escalated"] = True
            state["prior_model"] = prior
            state["escalated_at"] = now
            state["fable_alias"] = FABLE_MODEL_ALIAS
            state.pop("ask_pending", None)
            state.pop("last_ask_at", None)
            save()
            send(
                ":sparkles: Switched the core to Fable 5 and resumed. I'll ask "
                "to switch back once this is sorted."
            )
            return {"action": "escalated", "prior_model": prior}

        if action == "restore":
            if not state.get("escalated"):
                return {"action": "not_escalated"}
            prior = state.get("prior_model")  # None => clear the override
            if not restart_fn(prior):
                return {"action": "restart_failed", "mode": "restore"}
            state["escalated"] = False
            state["restored_at"] = now
            state.pop("prior_model", None)
            state.pop("escalated_at", None)
            state.pop("resolve_prompt_pending", None)
            save()
            send(
                ":arrows_counterclockwise: Switched the core back to "
                + (prior if prior else "the default model") + "."
            )
            return {"action": "restored", "restored_model": prior}

    return None


def check_once(
    state_file: Optional[Path] = None,
    now: Optional[float] = None,
    stuck_fn=None,
    resolved_fn=None,
    reply_fn=None,
    sender=None,
    restart_fn=None,
) -> "dict | None":
    """One pass of the state machine (the `--check` entrypoint). Drives the
    whole flow: prompts the owner when stuck-detected, performs the switch when
    the owner's post-ask reply is affirmative, prompts to switch back when the
    escalated topic looks resolved, and switches back when that reply is
    affirmative. All collaborators injectable.

    `reply_fn(since)` classifies the owner's latest message after `since` as
    "yes" / "no" / None (defaults to owner_reply_after). `restart_fn` / `sender`
    are threaded into escalate_to_fable / restore_prior_model so the switch the
    yes triggers stays hermetic under test.

    Runs the load -> decide -> save under the same non-blocking flock as
    recover_core so a manual --check and a cron tick can't both fire the ask."""
    if now is None:
        now = time.time()
    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "model-escalation.json"
    send = sender or _default_owner_dm
    reply = reply_fn or (lambda since: owner_reply_after(now, since))

    # escalate_to_fable / restore_prior_model take the same flock as this pass,
    # so when the owner's reply commands a switch we decide it inside the lock,
    # then perform the switch AFTER releasing the lock (below).
    with _locked_state(state_file) as ctx:
        if ctx is None:
            return {"action": "locked"}
        state, save = ctx

        if state.get("escalated"):
            if state.get("ask_pending"):
                # (never both; defensive) treat as escalated, drop stale ask.
                state.pop("ask_pending", None)
                save()

            if not state.get("resolve_prompt_pending"):
                # Watch for resolution -> prompt to switch back (once/episode).
                resolve = (resolved_fn or (
                    lambda: detect_resolved(now, state.get("topic", ""), state.get("escalated_at", 0.0))
                ))()
                if not resolve:
                    return {"action": "escalated_waiting"}
                ok = send(
                    f":white_check_mark: Looks like *{state.get('topic', 'that')}* is "
                    "sorted now. Switch the core back from Fable 5? (reply yes to switch back)"
                )
                if ok:
                    state["resolve_prompt_pending"] = True
                    state["resolve_prompt_at"] = now
                    save()
                return {"action": "resolve_prompted", "reason": resolve.get("reason")}

            # A switch-back prompt is pending: the owner's reply drives it.
            decision = reply(state.get("resolve_prompt_at", state.get("escalated_at", 0.0)))
            if decision == "yes":
                _pending_switch = "restore"  # perform after the lock releases
            elif decision == "no":
                # Owner declined the switch-back: stay on Fable, clear the
                # prompt so a later resolution can re-prompt.
                state.pop("resolve_prompt_pending", None)
                state.pop("resolve_prompt_at", None)
                save()
                return {"action": "restore_declined"}
            else:
                return {"action": "resolve_reply_pending"}
        else:
            # Not escalated. First, if an ASK is pending, the owner's reply to it
            # drives the escalate (or a decline).
            if state.get("ask_pending"):
                decision = reply(state.get("last_ask_at", 0.0))
                if decision == "yes":
                    _pending_switch = "escalate"  # perform after the lock
                elif decision == "no":
                    # Declined: clear the ask and record the decline so the same
                    # topic doesn't immediately re-ask this pass.
                    state.pop("ask_pending", None)
                    state["declined_topic"] = state.get("topic")
                    state["declined_at"] = now
                    save()
                    return {"action": "ask_declined", "topic": state.get("topic")}
                else:
                    _pending_switch = None  # no reply yet — may still re-ask below
            else:
                _pending_switch = None

            if _pending_switch is None:
                # No affirmative reply — look for a stuck topic to ASK about.
                stuck = (stuck_fn or (lambda: detect_stuck(now)))()
                if not stuck:
                    # Clear a stale pending-ask so a future topic starts fresh.
                    if state.get("ask_pending"):
                        state.pop("ask_pending", None)
                        save()
                    return None

                topic = stuck["topic"]
                last_ask = state.get("last_ask_at") or 0
                # Cooldown: don't re-ask every tick while the owner is deciding,
                # unless the topic changed (a new stuck problem deserves a prompt).
                if (
                    state.get("ask_pending")
                    and state.get("topic") == topic
                    and last_ask
                    and now - last_ask < ESCALATE_COOLDOWN_SEC
                ):
                    return {"action": "ask_cooldown", "topic": topic, "since_ask": int(now - last_ask)}

                ok = send(
                    f":hourglass_flowing_sand: It looks like *{topic}* keeps not "
                    "getting solved. Switch to Fable 5? (reply yes to switch)"
                )
                if not ok:
                    # DM failed — don't record the ask, so the next pass retries.
                    return {"action": "ask_failed", "topic": topic}
                state["ask_pending"] = True
                state["topic"] = topic
                state["last_ask_at"] = now
                state["repeat_count"] = stuck["repeat_count"]
                state["frustrated"] = stuck["frustrated"]
                save()
                return {"action": "asked", "topic": topic, "repeat_count": stuck["repeat_count"]}

    # Lock released. Perform the switch the owner's yes commanded.
    if _pending_switch == "escalate":
        result = escalate_to_fable(
            state_file=state_file, now=now, restart_fn=restart_fn, sender=send,
        )
        return {"action": "escalate_on_yes", "result": result}
    if _pending_switch == "restore":
        result = restore_prior_model(
            state_file=state_file, now=now, restart_fn=restart_fn, sender=send,
        )
        return {"action": "restore_on_yes", "result": result}
    return None


def _read_state(state_file: Optional[Path] = None) -> "dict":
    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "model-escalation.json"
    try:
        data = json.loads(state_file.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> int:
    if "--status" in sys.argv:
        print(json.dumps(_read_state(), indent=2))
        return 0
    if "--check" in sys.argv:
        result = check_once()
        print(json.dumps(result if result is not None else {"action": "none"}, indent=2))
        return 0
    if "--escalate" in sys.argv:
        # Manual override / concrete invocation point: switch to Fable 5 now.
        result = escalate_to_fable()
        print(json.dumps(result if result is not None else {"action": "none"}, indent=2))
        return 0
    if "--restore" in sys.argv:
        # Manual override: switch back from Fable 5 now.
        result = restore_prior_model()
        print(json.dumps(result if result is not None else {"action": "none"}, indent=2))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
