#!/usr/bin/env python3
"""Codebase-sweep regression guard: every known task-writing file must use the
injection guard.

Rather than testing the guard's behavior (that's task-body-guard.test.py and
task-body-injection-guard.test.py), this test checks that the CALLER SITES are
wired up. Add a new task writer? This test fails until you add the guard call.

Python path: import + call confine_user_content()
TypeScript path: define + call confineUserContent() (module-private — checked by source scan)

Run: python3 tests/injection-guard-sweep.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_passed = 0
_failed = 0


def _check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:  # pragma: no cover — failure path only runs when a test regresses
        _failed += 1
        print(f"FAIL [{label}]{': ' + detail if detail else ''}", file=sys.stderr)


def _src(path: str) -> str:
    return (REPO / path).read_text()


# ---------------------------------------------------------------------------
# Python bridge files: must import AND call confine_user_content
# ---------------------------------------------------------------------------

for _f in (
    "src/discord-bridge.py",
    "src/telegram-bridge.py",
    "src/slack-bridge.py",
    "src/github-webhook.py",
    "src/agent-api.py",
):
    _s = _src(_f)
    _name = _f.split("/")[-1]
    _check(
        f"{_name}: imports confine_user_content",
        "from task_body_guard import confine_user_content" in _s,
        f"{_f} must import confine_user_content from task_body_guard",
    )
    _check(
        f"{_name}: calls confine_user_content",
        "confine_user_content(" in _s,
        f"{_f} must call confine_user_content() on user-supplied task body",
    )

# discord-bridge: enriched task body must be re-confined after Discord-state prefetch
# (fetched channel messages are not run through confine before being prepended;
# an attacker-controlled channel could post ===SUTANDO SYSTEM INSTRUCTIONS===)
_db = _src("src/discord-bridge.py")
_check(
    "discord-bridge: confine_user_content(enriched) after prefetch",
    "user_task_text = confine_user_content(enriched)" in _db,
    "discord-bridge.py must re-apply confine_user_content to the enriched body after "
    "_prefetch_discord_state_refs. Fetched Discord channel messages are not confined, "
    "so an attacker-controlled channel can inject ===fence=== content into the task file.",
)

# ---------------------------------------------------------------------------
# Python bridges: task: field must come AFTER source/access_tier/priority
# (belt-and-suspenders alongside the ZWSP guard — a forged field in user content
# that slips past the guard would appear before the real fields, losing the race)
# ---------------------------------------------------------------------------

def _task_field_is_last_in_write_text(src: str) -> bool:
    """Check that task: appears after source/access_tier/priority in the
    task-file field template.

    The template is a contiguous run of f-string literal lines — either the
    inline `task_file.write_text(f"...\\n" ...)` block OR a builder function
    (e.g. discord-bridge's `_build_task_content()`, which returns the same
    concatenated f-strings so a build failure is logged instead of raising).
    We locate the run that contains an `f"task: "` line and check the field
    order within it, so the field-order defense is verified wherever the
    template lives — not only when it's inline at the write_text() call.

    (Anchoring solely on `task_file.write_text(` false-negatived after the
    discord-bridge callable refactor: the write_text arg became a bare
    variable, so zero inline keys were collected — 2026-07-14.)
    """
    fstr_line = re.compile(r'^\s*f"')          # an f-string literal template line
    key_pat = re.compile(r'f"([a-z_]+): ')     # a `key: ...` field line
    run: list[str] = []                        # keys in the current contiguous run
    keys: list[str] = []
    for line in src.splitlines():
        if fstr_line.match(line):
            m = key_pat.search(line)
            if m:
                run.append(m.group(1))
        else:
            # A non-f-string line ends the run. Keep it iff it's the run that
            # holds the task template; otherwise it wasn't the template — reset.
            if "task" in run:
                keys = run
                break
            run = []
    else:  # file ended mid/at a template run (no trailing non-f-string line)
        if "task" in run:
            keys = run
    if "task" not in keys:
        return False
    task_pos = keys.index("task")
    for sentinel in ("source", "access_tier", "priority"):
        if sentinel in keys and keys.index(sentinel) > task_pos:
            return False
    return True


for _fb, _fname in (
    ("src/discord-bridge.py", "discord-bridge"),
    ("src/telegram-bridge.py", "telegram-bridge"),
    ("src/slack-bridge.py", "slack-bridge"),
):
    _sb = _src(_fb)
    _check(
        f"{_fname}: task: field is after source/access_tier/priority (field-order defense)",
        _task_field_is_last_in_write_text(_sb),
        f"{_fb}: task: must appear after source/access_tier/priority in task_file.write_text()",
    )

# ---------------------------------------------------------------------------
# github-webhook.py: access_tier: other MUST appear before task: (security-critical)
# External GitHub events are untrusted — owner-tier processing must be impossible
# even if confine_user_content is somehow bypassed.
# ---------------------------------------------------------------------------

_gh = _src("src/github-webhook.py")
# Find the task_content block
_gh_tc_start = _gh.find("task_content = (")
_gh_tc_end = _gh.find(").write_text(", _gh_tc_start) if _gh_tc_start > 0 else -1
_gh_tc_block = _gh[_gh_tc_start:_gh_tc_end + 30] if _gh_tc_start > 0 and _gh_tc_end > 0 else ""
_check(
    "github-webhook: access_tier: other present in task_content",
    '"access_tier: other' in _gh_tc_block or "'access_tier: other'" in _gh_tc_block
    or "access_tier: other" in _gh_tc_block,
    "github-webhook.py task_content must set access_tier: other — external GitHub "
    "events must NEVER be elevated to owner-tier processing",
)
_check(
    "github-webhook: access_tier: other appears before task: in task_content",
    "access_tier" in _gh_tc_block and "task:" in _gh_tc_block
    and _gh_tc_block.index("access_tier") < _gh_tc_block.index('"task:'),
    "github-webhook.py: access_tier: other must precede task: in task_content "
    "(belt-and-suspenders: a ZWSP bypass won't let injected content raise the tier)",
)

# ---------------------------------------------------------------------------
# Python ag2-relay: uses _one_line() as structural equivalent
# (collapses newlines → prevents line-based injection; no ZWSP needed)
# ---------------------------------------------------------------------------

_ag2_path = REPO / "skills/ag2-relay/remote-task-client.py"
if _ag2_path.exists():
    _ag2 = _ag2_path.read_text()
    _check(
        "ag2-relay: _one_line on task fields",
        "_one_line(" in _ag2 and "task" in _ag2,
        "ag2-relay must use _one_line() for newline-stripping on task fields",
    )
    _check(
        "ag2-relay: access_tier appended last (local decision wins)",
        "lines.append(f\"access_tier: {LOCAL_TIER}\")" in _ag2
        or 'lines.append(f"access_tier: {LOCAL_TIER}")' in _ag2
        or "access_tier" in _ag2 and "last" in _ag2.lower(),
    )

# ---------------------------------------------------------------------------
# TypeScript task-bridge.ts: confineUserContent defined and applied at all sites
# ---------------------------------------------------------------------------

_tb = _src("src/task-bridge.ts")
_check(
    "task-bridge: confineUserContent defined",
    "function confineUserContent" in _tb,
)
_check(
    "task-bridge: confineUserContent(task) — voice body",
    "confineUserContent(task)" in _tb,
)
_check(
    "task-bridge: confineUserContent(recent) — voice transcript",
    "confineUserContent(recent)" in _tb,
)
_check(
    "task-bridge: confineUserContent(content) — context-drop",
    "confineUserContent(content)" in _tb,
)
_check(
    "task-bridge: confineUserContent(taskDescription) — chat path",
    "confineUserContent(taskDescription)" in _tb,
)

# ---------------------------------------------------------------------------
# TypeScript conversation-server.ts: confineUserContent defined and applied
# ---------------------------------------------------------------------------

_cs = _src("skills/phone-conversation/scripts/conversation-server.ts")
_check(
    "conversation-server: confineUserContent defined",
    "function confineUserContent" in _cs,
)
_check(
    "conversation-server: confineUserContent(taskDescription) — delegation",
    "confineUserContent(taskDescription)" in _cs,
)
_check(
    "conversation-server: confineUserContent(fullTranscript) — delegation",
    "confineUserContent(fullTranscript)" in _cs,
)
_check(
    "conversation-server: confineUserContent(formatted) — summary task",
    "confineUserContent(formatted)" in _cs,
)
_check(
    "conversation-server: confineUserContent(callerNumber) — caller field before access_tier",
    "confineUserContent(callSession.callerNumber" in _cs,
    "caller: field appears before access_tier: in delegateTask() — callerNumber must be confined "
    "so a \\naccess_tier: owner injection lands ZWSP-prefixed (Twilio validates signatures but "
    "defence-in-depth requires this)",
)
_check(
    "conversation-server: confineUserContent(session.callerNumber) — summary task caller field",
    "confineUserContent(session.callerNumber" in _cs,
    "caller: field appears before access_tier: in the summary task (lines ~1264) — session.callerNumber "
    "must be wrapped in confineUserContent() for the same reason as delegateTask(): a \\naccess_tier: owner "
    "injection in the phone number would precede the real access_tier: line.",
)

# ---------------------------------------------------------------------------
# inline-tools.ts cancel_task: targetId newline-stripped, task: field last
# ---------------------------------------------------------------------------

_it = _src("src/inline-tools.ts")
# Locate the cancelBody block
_cb_start = _it.find("const cancelBody = [")
_cb_end = _it.find("].join('\\n');", _cb_start) if _cb_start > 0 else -1
_cb_block = _it[_cb_start:_cb_end + 20] if _cb_start > 0 and _cb_end > 0 else ""
_check(
    "inline-tools: cancel_task strips newlines from targetId",
    "safeTargetId" in _it and ".replace(/[\\r\\n]/g, '')" in _it,
    "cancel_task embeds targetId (Gemini-controlled) in the task: body — must strip newlines "
    "before embedding so a forged \\naccess_tier: line can't precede the real one",
)
_check(
    "inline-tools: cancel_task task: field is last (after access_tier:)",
    "access_tier: owner" in _cb_block
    and "task: CANCEL_INSTRUCTION" in _cb_block
    and _cb_block.index("access_tier: owner") < _cb_block.index("task: CANCEL_INSTRUCTION"),
    "cancel_task cancelBody must place task: after access_tier: (field-order defence)",
)

# ---------------------------------------------------------------------------
# agent-api.py Twilio handlers: caller/sender must be confined in from: AND task: body
# (SMS and transcription handlers embed caller/sender in the task body — unconfined
# values could inject ===fence=== patterns or header-key lines into the task body)
# ---------------------------------------------------------------------------

_aa = _src("src/agent-api.py")
_check(
    "agent-api: twilio voice — safe_caller used in from: and task: body",
    "safe_caller = confine_user_content(caller)" in _aa
    and "from: {safe_caller}" in _aa,
    "agent-api.py handle_twilio_voice must define safe_caller and use it in both "
    "the from: field and the task: body (caller appears in both)",
)
_check(
    "agent-api: twilio SMS — safe_sender used in from: and task: body",
    "safe_sender = confine_user_content(sender)" in _aa
    and "from: {safe_sender}" in _aa
    and "SMS from {safe_sender}" in _aa,
    "agent-api.py handle_twilio_sms must confine sender in both from: field and "
    "task: body (prior code had unconfined {sender} in 'SMS from {sender}: ...')",
)
_check(
    "agent-api: twilio voicemail — safe_caller used in from: and task: body",
    # voicemail handler uses safe_caller in from: and task: body
    _aa.count("safe_caller = confine_user_content(caller)") >= 2,
    "agent-api.py handle_twilio_transcription must also define safe_caller "
    "(two handlers use this pattern — check both voice and transcription)",
)
# All four agent-api.py task writers must declare access_tier: owner explicitly
# before task: — self-documenting and immune to a future default-tier change.
# Anchor on the f-string source field literal so we hit the task_content block,
# not the function-name or comment that contains the same substring.
for _src_literal, _src_label in (
    ('"source: twilio_voice\\n"',   "twilio_voice"),
    ('"source: twilio_sms\\n"',     "twilio_sms"),
    ('"source: twilio_voicemail\\n"', "twilio_voicemail"),
    ('"source: api\\n"',            "api"),
):
    _tw_start = _aa.find(_src_literal)
    _tw_task  = _aa.find("task:", _tw_start) if _tw_start >= 0 else -1
    _tw_tier  = _aa.find("access_tier: owner", _tw_start) if _tw_start >= 0 else -1
    _check(
        f"agent-api: {_src_label} — access_tier: owner before task:",
        _tw_start >= 0 and _tw_tier >= 0 and _tw_task >= 0 and _tw_tier < _tw_task,
        f"agent-api.py {_src_label} task_content must declare access_tier: owner "
        f"before task: — makes trust level self-documenting and safe if the implicit "
        f"default ever changes",
    )

# ---------------------------------------------------------------------------
# web-client.ts: task body is hardcoded (no user data embedded)
# ---------------------------------------------------------------------------

_wc = _src("src/web-client.ts")
# Find the server-side handler by locating the writeFileSync call that follows
# the /paidsubscriptions/scan comment block (scan-prompt.md pointer pattern).
_wc_server_start = _wc.find("scan-prompt.md")
_wc_task_block = _wc[max(0, _wc_server_start - 400): _wc_server_start + 600] if _wc_server_start > 0 else ""
# Confirm the task content is a hardcoded literal, not user-interpolated
_check(
    "web-client: paidsubscriptions task is hardcoded (no ${req.} interpolation)",
    "scan-prompt.md" in _wc_task_block and "${req." not in _wc_task_block,
    "web-client /paidsubscriptions/scan task must use hardcoded content only",
)

# ---------------------------------------------------------------------------
# Exhaustive scan: every Python file that embeds an interpolated task: field
# (pattern: f"task: {) must be in the known-guarded set.  This acts as a
# trip-wire — a new task writer that forgets to call confine_user_content()
# fails here before it ships, rather than being discovered later via manual
# code review.
#
# health-check.py is deliberately absent: its task: line is a hardcoded
# string literal with no {interpolation}, so user content never lands there
# and the pattern does not match.
# ag2-relay uses f"{field}: {_one_line(value)}" not f"task: {" directly, so
# it also does not appear in this scan (it is covered by its own check above).
# ---------------------------------------------------------------------------

_GUARDED_PY_WRITERS = {
    # The centralized write side (serialize_task_last). Its guard is
    # structural, not ZWSP-defang: header values reject newlines outright and
    # the body is serialized after the single task: line, so the task-last
    # parser can never promote body content to a header.
    "src/local_task_protocol.py",
    "skills/schedule-crons/scripts/codex-scheduler.py",
    "src/discord-bridge.py",
    "src/telegram-bridge.py",
    "src/slack-bridge.py",
    "src/github-webhook.py",
    "src/agent-api.py",
    "src/cron-runner.py",
    # Room speech + quoted article text: every value goes through confine(),
    # and task: is written last so body newlines cannot forge fields.
    "src/signal_room_tasks.py",
}

_TASK_FIELD_PATTERN = 'f"task: {'
for _pyf in sorted(
    list((REPO / "src").glob("*.py")) + list((REPO / "skills").rglob("*.py"))
):
    _rel = str(_pyf.relative_to(REPO))
    try:
        _fc = _pyf.read_text(errors="replace")
    except OSError:  # pragma: no cover — error handler
        continue
    if "__pycache__" in _rel or _TASK_FIELD_PATTERN not in _fc:
        continue
    _check(
        f"exhaustive-scan: {_rel} is a known-guarded task writer",
        _rel in _GUARDED_PY_WRITERS,
        f"{_rel} contains an interpolated task: f-string but is not in "
        f"_GUARDED_PY_WRITERS.  Add confine_user_content() to its task body, "
        f"then add the path to _GUARDED_PY_WRITERS so future passes catch regressions.",
    )

# ---------------------------------------------------------------------------
# Exhaustive scan (TypeScript): every TS file containing "task: ${"
# (template-literal interpolated task field) must be in the known-guarded set.
#
# inline-tools.ts cancel_task uses "task: CANCEL_INSTRUCTION: ... ${safeTargetId}"
# — the pattern "task: ${" does NOT match because there is static text between
# "task: " and "${safeTargetId}"; it is covered by the specific cancelBody
# assertions above.
# web-client.ts uses a hardcoded task body (no ${req.} interpolation); also
# covered by its own assertion above.
# ---------------------------------------------------------------------------

_GUARDED_TS_WRITERS = {
    "src/task-bridge.ts",
    "skills/phone-conversation/scripts/conversation-server.ts",
}

_TS_TASK_FIELD_PATTERN = "task: ${"
for _tsf in sorted(
    list((REPO / "src").glob("*.ts")) + list((REPO / "skills").rglob("*.ts"))
):
    _rel = str(_tsf.relative_to(REPO))
    if ".d.ts" in _rel or "test" in _rel:
        continue
    try:
        _fc = _tsf.read_text(errors="replace")
    except OSError:  # pragma: no cover — error handler
        continue
    if _TS_TASK_FIELD_PATTERN not in _fc:
        continue
    _check(
        f"exhaustive-scan-ts: {_rel} is a known-guarded TS task writer",
        _rel in _GUARDED_TS_WRITERS,
        f"{_rel} contains an interpolated task: template literal but is not in "
        f"_GUARDED_TS_WRITERS.  Add confineUserContent() to its task body, "
        f"then add the path to _GUARDED_TS_WRITERS so future passes catch regressions.",
    )

# ---------------------------------------------------------------------------
# Header-key parity: _HEADER_KEYS in task_body_guard.py, task-bridge.ts, and
# conversation-server.ts must contain the same 14 keys.  Drift between
# implementations creates blind spots — a key guarded in Python but not TS
# (or vice versa) leaves one attack surface unprotected.
# ---------------------------------------------------------------------------

_tg = _src("src/task_body_guard.py")
_py_keys_m = re.search(r"_HEADER_KEYS\s*=\s*\(([^)]+)\)", _tg, re.DOTALL)
_py_header_keys: set[str] = set()
if _py_keys_m:
    _py_header_keys = {
        k.strip().strip("\"'")
        for k in _py_keys_m.group(1).split(",")
        if k.strip().strip("\"' ")
    }
else:
    # The guard imports its keys from local_task_protocol.KNOWN_HEADER_KEYS
    # (single source of truth — no hardcoded literal to scrape). Read that
    # tuple directly so the parity check still has the authoritative py set.
    _ltp = _src("src/local_task_protocol.py")
    # Close-paren is anchored to line start (\n)) so a ')' inside an inline
    # comment can't truncate the tuple early.
    _ltp_m = re.search(r"KNOWN_HEADER_KEYS\s*=\s*\((.*?)\n\)", _ltp, re.DOTALL)
    if _ltp_m:
        # Drop comment lines before scraping quoted tokens.
        _body = "\n".join(
            ln for ln in _ltp_m.group(1).splitlines()
            if not ln.lstrip().startswith("#")
        )
        _py_header_keys = set(re.findall(r"[\"']([^\"']+)[\"']", _body))

_tb2 = _src("src/task-bridge.ts")
_ts_keys_m = re.search(r"const _HEADER_KEYS\s*=\s*\[([^\]]+)\]", _tb2, re.DOTALL)
_ts_header_keys: set[str] = set()
if _ts_keys_m:
    _ts_header_keys = {
        k.strip().strip("\"', ")
        for k in _ts_keys_m.group(1).split(",")
        if k.strip().strip("\"', ")
    }

_check(
    "header-key-parity: task_body_guard.py _HEADER_KEYS matches task-bridge.ts",
    bool(_py_header_keys) and _py_header_keys == _ts_header_keys,
    f"key mismatch: py={sorted(_py_header_keys - _ts_header_keys)} "
    f"ts={sorted(_ts_header_keys - _py_header_keys)} — "
    "add missing keys to both implementations to keep guards in sync",
)

_cs2 = _src("skills/phone-conversation/scripts/conversation-server.ts")
_cs_missing = [k for k in _py_header_keys if k not in _cs2]
_check(
    "header-key-parity: conversation-server.ts _CONF_HEADER_RE contains all py keys",
    not _cs_missing,
    f"keys missing from conversation-server.ts _CONF_HEADER_RE: {_cs_missing} — "
    "update the inline regex string to include them",
)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

_total = _passed + _failed
print(f"injection-guard-sweep: {_passed}/{_total} passed"  # expected 48/48
      + ("" if _failed == 0 else f" — {_failed} FAILED"))
sys.exit(0 if _failed == 0 else 1)
