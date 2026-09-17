#!/usr/bin/env python3
"""Check pending questions and notify if unanswered.

Runs on cron — independent of the proactive loop.
Sends notifications via macOS + Discord DM if questions are waiting.
Use --force to bypass the 1-hour cooldown.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import personal_path  # noqa: E402
from pending_questions_md import active_region  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
from presenter_mode import presenter_mode_active  # noqa: E402

WORKSPACE = resolve_workspace()
PQ_FILE = Path(personal_path("pending-questions.md", WORKSPACE))
RESULTS_DIR = WORKSPACE / "results"
# No read-fallback to the old root path on purpose: a missing stamp makes the
# reader notify ONCE rather than suppress, so the move costs one notification.
LAST_NOTIFY_FILE = WORKSPACE / "state" / "last-pq-notify"


def write_notify_stamp(questions, now=None):
    """Record that this question set was just notified.

    Named so it is testable without driving `main`, which fires a real notification.
    """
    LAST_NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = int(time.time()) if now is None else now
    LAST_NOTIFY_FILE.write_text(f"{ts} {notify_key(questions)}")
    # Retire AFTER the new stamp exists: a crash between the two costs at most a
    # cooldown. Path derived from LAST_NOTIFY_FILE so a redirected test stays in tmp.
    try:
        (LAST_NOTIFY_FILE.parent.parent / ".last-pq-notify").unlink(missing_ok=True)
    except OSError:
        pass
VOICE_LOG = WORKSPACE / "logs" / "voice-agent.log"
# How long an UNCHANGED question set stays quiet before it is raised again. This
# is the floor that stops "notify only when the set changes" from turning an
# unanswered queue permanently mute — see should_notify().
UNCHANGED_REMINDER_SEC = 86400  # 24h


def voice_client_connected():
    """True if the most recent [Health] line in voice-agent.log shows client=true.
    When the voice client is offline, dm-fallback already delivers question-*.txt
    files via Discord DM — writing one would double-DM with notify_discord_dm."""
    if not VOICE_LOG.exists():
        return False
    try:
        # Read the tail efficiently: open at end, walk back ~16KB
        with VOICE_LOG.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode('utf-8', errors='replace')
        for line in reversed(tail.splitlines()):
            if '[Health]' in line and 'client=' in line:
                return 'client=true' in line
    except Exception:
        pass
    return False


# A `## ` heading is not always a question. Two forms are structural, and both
# must be classified HERE rather than by each consumer, so the notifier and the
# briefing cannot report different counts for the same file.
#
# Both rules are anchored to SHAPE, not to a keyword appearing somewhere. Earlier
# versions matched the word and each one deleted a live, `Status: open` question:
#
#   `^HELD\b`            -> "## HELD deployment until the owner approves the
#                            migration" is a real ask, not a section shell.
#   `.search()` for the
#   inline marker        -> "## Confirm whether the UI should render a [DONE]
#                            badge" is a question ABOUT a badge.
#
# So: an organizer shell is a keyword followed by a separator ("## ACTIVE — …",
# "## FRESH – …", "## HELD: …") — a grouping label, never a sentence. And a
# resolution marker is a bracketed group at the START of the title
# ("## [RESOLVED 2026-07-03] shipped"), never one mentioned mid-sentence.
_ORG_HEADING = re.compile(
    r'^(FRESH|ACTIVE|HELD|TRIAGE|SURFACED|RESOLVED|ANSWERED)\s*(?:[—–\-:]|$)',
    re.IGNORECASE,
)
# Anchored with ^ and \s* — a marker leads the title or it is not a marker. The
# closed-bracket grammar (keyword then `]` or whitespace-then-content-then-`]`)
# rejects `[RESOLVED?]` / `[done-ish]`, which named an open uncertainty.
# `(?:\d+[.)]\s*)?` — real entries carry an enumeration prefix
# ("## 2. [RESOLVED 2026-07-03] shipped already"), so the marker leads the title
# CONTENT, not necessarily character 0. Anchoring at character 0 alone dropped
# that form (caught by tests/morning-briefing-pending-extract.test.py). It stays
# anchored otherwise: "render a [DONE] badge" has the bracket mid-sentence and is
# still a live question.
_INLINE_RESOLVED = re.compile(
    r'^\s*(?:\d+[.)]\s*)?\[\s*(?:✅\s*)?(?:RESOLVED|DONE|ANSWERED)(?:\s[^\]]*)?\]',
    re.IGNORECASE,
)


def section_is_waiting(title: str, body: str) -> bool:
    """One rule for "this entry still wants an answer", used on BOTH regions.

    `zero_reason()` asks it about the archive; a second rule there would let one
    entry read as resolved in one region and open in the other.

    `open` is the word writers naturally reach for, and it used to fall through
    to the resolved skip — filing a live question as though it were answered.
    The section stayed on disk and readable while never being surfaced, which is
    the worst failure mode here.
    """
    if not title or _ORG_HEADING.match(title) or _INLINE_RESOLVED.match(title):
        return False
    status_m = re.search(r'\*\*Status:\*\*\s*(.+)', body)
    if status_m:
        return status_m.group(1).strip().lower().startswith(
            ('unanswered', 'waiting', 'open'))
    return True  # no status field: free-form prose is unanswered by convention


def get_waiting_questions():
    """Parse pending-questions.md — matches the legacy `## Q1 — Title` and
    `## Title` / `- **Status:** unanswered` section formats AND the free-form
    `- **[label, ts]** ...` bullet format the proactive-loop writes in practice.

    If a section has no explicit **Status:** marker, it is treated as
    unanswered (the free-form prose format used in practice never writes
    a status field; sections are deleted when resolved, not marked done).
    Sections with an explicit status of "resolved" / "done" / "answered"
    are skipped so the old structured format still works correctly.
    """
    if not PQ_FILE.exists():
        return []
    content = PQ_FILE.read_text()
    # Only the active region counts. Resolved questions are kept below a
    # top-level "# Resolved" divider (audit trail), not deleted — without
    # this cut the heading-agnostic split below sweeps the whole file and
    # every resolved entry is miscounted as pending, re-notifying the owner
    # about already-answered questions. No-op when there is no such divider.
    content = active_region(content)
    questions = []
    # Walk each ## section; a section is waiting if its body contains
    # `Status: unanswered`, `Status: Waiting` or `Status: open`, OR has no
    # Status field at all (free-form prose sections are always unanswered by
    # convention).
    sections = re.split(r'^## ', content, flags=re.MULTILINE)
    for sec in sections[1:]:  # skip pre-header
        title_line, _, body = sec.partition('\n')
        title = title_line.strip()
        if not title:
            continue
        if not section_is_waiting(title, body):
            continue
        # Capture first non-empty, non-strikethrough, non-status-metadata body
        # line as a one-line action hint so notifications tell the user what
        # to do, not just that something is waiting (avoids "what do I do
        # with this?" confusion). Status metadata is skipped too — a section
        # whose **Status:** line comes before the narrative text would
        # otherwise DM "**Status:** unanswered" as the "action hint", which
        # tells the user nothing they don't already know from the ping itself.
        snippet_lines = [
            l.strip() for l in body.strip().splitlines()
            if l.strip() and not l.strip().startswith('~~')
            and not re.match(r'^(\*\*)?Status:(\*\*)?', l.strip(), re.IGNORECASE)
        ]
        snippet = snippet_lines[0][:120] if snippet_lines else ""
        # `body` is the FULL section text. `snippet` is a 120-char action hint and
        # `title` a heading, so a caller checking "did my question land?" against
        # them can only ever match the first ~100 characters of an entry. The
        # documented verification in the proactive-loop skill does exactly that:
        #   any('<phrase>' in str(q) for q in get_waiting_questions())
        # and its own text calls a True "the only proof the question exists". A
        # phrase further into the entry made that return False for a question that
        # was filed, above the divider, and counted — a verification step whose
        # failure mode is reporting the healthy case as broken.
        questions.append({"id": title[:40], "title": title, "snippet": snippet,
                          "body": body.strip()})

    # Also recognize the free-form bullet format the proactive-loop and skills
    # actually append in: `- **[label, timestamp]** ...`. The `## `-section walk
    # above misses these entirely (real pending-questions.md carries 0 `## `
    # headings, only bullets), which silently zeroed the count and suppressed
    # every notification. Bullets follow the same "no Status field ⇒ unanswered"
    # convention as prose sections (resolved items are deleted, not marked).
    seen = {q["title"] for q in questions}
    for m in re.finditer(r'^\s*-\s+\*\*\[(.+?)\]', content, flags=re.MULTILINE):
        title = m.group(1).strip()
        if title and title not in seen:
            seen.add(title)
            # `title` is only the BRACKETED LABEL, so bodying to it would leave the
            # rest of the bullet — where the actual ask lives — just as unsearchable
            # as the section case this change exists to fix. Take the whole line.
            # Anchor off m.end(), not m.start(): `^\s*` lets \s match the preceding
            # NEWLINE, so on a bullet with a blank line above it the match begins on
            # that blank line and a start-anchored slice comes back empty.
            line_start = content.rfind("\n", 0, m.end()) + 1
            line_end = content.find("\n", m.end())
            stop = line_end if line_end != -1 else len(content)
            body = content[line_start:stop].strip()
            # The DM renders `snippet`, not `body`; an empty one delivered the
            # bracketed label alone, so options and defaults never reached anyone.
            ask = content[m.end():stop].strip().lstrip("*").strip()
            questions.append({"id": title[:40], "title": title,
                              "snippet": ask[:120], "body": body or title})
    return questions


def _last_notify_state():
    """(mtime, key) of the last notification. `key` is None for the pre-2026-08-01
    format, which stored a bare timestamp — so the first run after upgrading is
    treated as "set unknown" and notifies once rather than silently suppressing."""
    if not LAST_NOTIFY_FILE.exists():
        return None, None
    mtime = LAST_NOTIFY_FILE.stat().st_mtime
    parts = LAST_NOTIFY_FILE.read_text().split()
    return mtime, (parts[1] if len(parts) > 1 else None)


def should_notify(key=None):
    """Notify when the SET changed, when it is genuinely new, or when an unchanged
    set has gone unmentioned for longer than UNCHANGED_REMINDER_SEC.

    The old rule was purely time-based — 3600s since the marker's mtime, with no
    awareness of whether anything had changed — so an unchanged queue re-notified
    every hour, forever. Observed 2026-08-01: the identical 17 items reached the
    owner three times inside 60 minutes (05:43 cron, 06:17 briefing, 06:4x cron),
    content hash unchanged across all three.

    The script already computed the discriminator: `questions_key()` hashes the
    sorted titles and was used to name the proactive file. The cooldown simply
    never consulted it.

    A FLOOR, NOT A CLIFF (2026-08-01, Mini's cold review). The first version of
    this fix ended at `key != last_key`, which discarded mtime on that path — so
    an unchanged set was announced exactly once, EVER. That is wrong in the case
    the file exists for: questions are unchanged precisely BECAUSE nobody has
    answered them, and one host already carries 54 such items. They would have
    gone permanently silent, with no error — a queue that stops asking. Keeping
    the daily floor bounds the spam (the bug above was 3 sends in 60 min) while
    the queue stays audible.

    `key=None` preserves the old time-only rule. No production caller passes it —
    the only live call site hashes the set — so treat it as a compatibility
    default for an embedder, not as a path this repo exercises."""
    mtime, last_key = _last_notify_state()
    if mtime is None:
        return True                      # never notified
    if key is None:
        return (time.time() - mtime) > 3600
    if last_key is None:
        return True                      # legacy marker: key unknown, notify once
    if key != last_key:
        return True                      # the set changed
    # Unchanged set: quiet, but not forever — re-raise once a day so an
    # unanswered queue keeps asking instead of going mute.
    return (time.time() - mtime) > UNCHANGED_REMINDER_SEC


#: Body budget for the macOS notification. Not a hard OS limit — a chosen bound
#: the assembled body is held under, so no count width can overrun it.
BODY_MAX = 160


def notify_macos(count, titles):
    """Returns True only if osascript actually accepted the notification."""
    # macOS truncates the body: the [:40] cap is the bound, since a title need
    # not contain a comma; blanks are dropped so the join cannot emit a bare `, ,`.
    names = [n for n in (t.split(",", 1)[0].strip()[:40] for t in titles[:3]) if n]
    extra = f" (+{count - len(names)} more)" if count > len(names) else ""
    head = f"{count} pending question{'s' if count > 1 else ''}: "
    # Cap the ASSEMBLED body, not just each name: the count and the overflow both
    # widen with the queue, so per-name bounds alone leave the total arithmetic.
    room = BODY_MAX - len(head) - len(extra) - 1
    joined = ", ".join(names)
    if room <= 0:
        joined = ""
    elif len(joined) > room:
        joined = joined[:room - 1] + "…"
    # When every candidate name is blank there is nothing between the colon and the
    # overflow, and `head` already ends in a space — so join on the stripped head.
    msg = f"{head}{joined}{extra}" if joined else f"{head.rstrip()}{extra}"
    # AppleScript string literal: backslashes and double quotes in question
    # titles must be escaped, or osascript rejects the script and the
    # notification silently reports FAILED (bit us 2026-07-26 — a title
    # containing a quoted phrase broke every fire while it sat in the top 3).
    esc = msg.replace("\\", "\\\\").replace('"', '\\"')
    try:
        r = subprocess.run([
            "osascript", "-e",
            f'display notification "{esc}" with title "Sutando"'
        ], capture_output=True)
    except (FileNotFoundError, OSError):
        return False
    return r.returncode == 0


def questions_key(questions):
    """sha256[:16] of the sorted question titles -- a stable id for the set."""
    key = "|".join(sorted(q["title"] for q in questions))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# Deepest ordered prefix any consumer renders: notify_macos shows titles[:3],
# the proactive DM body shows questions[:5]. Covering 5 covers both.
VISIBLE_PREFIX = 5


def notify_key(questions):
    """sha256[:16] of what the owner would actually SEE — set AND visible order.

    Deliberately NOT `questions_key`, which answers a different question. That one
    identifies the SET and must stay order-independent: it names the proactive file
    (`proactive-pending-q-<key>.txt`), so a reordered-but-identical set has to
    collapse onto the same filename instead of delivering a second copy. Pinned by
    tests/check-pending-questions-collapse.test.py.

    The cooldown asks something else: "would this fire show him anything new?"
    Both renders are ORDERED prefixes, so the set hash is wrong in both directions —
    promoting an item into the top 3 changes every rendered word while the hash holds
    (suppressed, and a promotion is deliberate precisely because the top slot should
    change), and adding a 21st item below the fold changes the hash while the rendered
    text is identical (fires, showing nothing new).

    Composed from `questions_key` rather than replacing it, so every membership change
    that notified before still notifies: this can only ever widen, never suppress.
    """
    visible = "|".join(q["title"] for q in questions[:VISIBLE_PREFIX])
    seed = f"{questions_key(questions)}#{visible}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def notify_voice(questions):
    """Write to results/ so voice agent can speak it."""
    ts = int(time.time() * 1000)
    path = RESULTS_DIR / f"question-{ts}.txt"
    titles = [q["title"] for q in questions]
    path.write_text(
        f"You have {len(questions)} pending question{'s' if len(questions) > 1 else ''} waiting for your answer: "
        + "; ".join(titles)
        + ". Check the Questions tab in the web UI."
    )


def notify_discord_dm(questions):
    """Write a proactive-*.txt file so discord-bridge DMs the owner.
    Owner asked (2026-04-09, while traveling) to receive pending-question
    pings as DMs instead of just macOS notifications."""
    path = RESULTS_DIR / f"{PROACTIVE_PREFIX}{questions_key(questions)}.txt"
    lines = [
        f"⚠️ {len(questions)} pending question{'s' if len(questions) > 1 else ''} waiting:",
        "",
    ]
    for q in questions[:5]:
        lines.append(f"• {q['title']}")
        if q.get("snippet"):
            lines.append(f"  ↳ {q['snippet']}")
    if len(questions) > 5:
        lines.append(f"…and {len(questions) - 5} more")
    lines.append("")
    lines.append(
        f"Reply here or edit pending-questions.md on {socket.gethostname().split('.')[0]} to resolve."
    )
    # Each body is a whole snapshot, so a stale one is wrong, not redundant. Look
    # BEFORE writing: a file appearing after can be an overlapping run's, not ours.
    superseded = [p for p in RESULTS_DIR.glob(f"{PROACTIVE_PREFIX}*.txt") if p != path]
    # Appear at the deliverable name in one step, from a scratch name no other run
    # can hold: a poll claims proactive-*.txt on sight and would DM a partial body.
    fd, tmp_name = tempfile.mkstemp(dir=RESULTS_DIR, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for old in superseded:
        old.unlink(missing_ok=True)


# A proactive-*.txt is only a DELIVERY if some bridge drains it. On a host where
# none is running the file just accumulates, while this script still printed
# "Notified" -- claiming an outcome it never achieved. Rather than sniff for
# consumer processes (pgrep -f self-matches; see the watcher notes), use the
# evidence already on disk: files we wrote earlier that nobody took.
UNDRAINED_AGE_S = 600
# Only OUR files are evidence about OUR delivery path. results/proactive-*.txt is
# a shared namespace — morning-briefing and the durable scheduler write there too
# (see notes/proactive-delivery-void-inventory.md). One unrelated stale file would
# otherwise produce a confident, wrong "the DM path is not reaching the owner".
PROACTIVE_PREFIX = "proactive-pending-q-"


def undrained_proactive_files():
    """Previously-written proactive-*.txt older than UNDRAINED_AGE_S -- i.e. old
    enough that a live consumer would have drained them."""
    now = time.time()
    out = []
    try:
        for f in RESULTS_DIR.glob(f"{PROACTIVE_PREFIX}*.txt"):
            try:
                if now - f.stat().st_mtime > UNDRAINED_AGE_S:
                    out.append(f.name)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(out)


def notify_summary(count, macos_ok, voice_ok, stale):
    """Build the per-path summary line, plus a warning when the DM path is dead.

    Pure so the claim itself is testable — the whole point of this change is that
    the summary must not assert delivery that did not occur."""
    paths = [
        "macos=ok" if macos_ok else "macos=FAILED",
        "voice=ok" if voice_ok else "voice=skipped(not connected)",
    ]
    if stale:
        paths.append(f"proactive-file=written but {len(stale)} earlier one(s) UNDRAINED")
    else:
        paths.append("proactive-file=written")
    summary = f"Notified: {count} pending questions [{', '.join(paths)}]"
    warning = None
    if stale:
        warning = (
            "  WARNING: no consumer is draining results/proactive-*.txt on this host "
            f"(oldest undrained: {stale[0]}). The DM path is NOT reaching the owner; "
            "only the macOS notification is real here."
        )
    return summary, warning


def deliver(questions, count, titles):
    """Fire every notification path and report what actually happened.

    Separated from main() so the delivery decisions are testable; main() is left
    as argument parsing plus printing. Voice is skipped when disconnected because
    the DM fallback would otherwise deliver question-*.txt as a duplicate.
    """
    stale = undrained_proactive_files()
    macos_ok = notify_macos(count, titles)
    voice_ok = False
    if voice_client_connected():
        notify_voice(questions)
        voice_ok = True
    notify_discord_dm(questions)
    summary, warning = notify_summary(count, macos_ok, voice_ok, stale)
    if warning:
        print(warning, file=sys.stderr)
    return summary


def _active_region_lost(active_text: str) -> bool:
    """True when the active-region HEADER is absent, not merely empty.

    Below the divider, resolution is expressed by POSITION, so "this entry lacks
    a resolved marker" is not evidence. What a swept file cannot fake is having no
    top-level heading left above the divider at all.

    Takes the ALREADY-PARSED active region: re-splitting here would be a second
    definition of the divider, which `active_region()` alone owns.
    """
    return not re.search(r'^#\s+\S', active_text, flags=re.MULTILINE)


def zero_reason():
    """Explain a zero so a parse fault cannot look like a quiet day.

    Every other early return in main() prints something; this one did not, and on
    2026-07-30 that cost ~11 hours. A divider-anchor bug made the active region
    collapse to the file's own header, `get_waiting_questions()` returned 0 while 43
    questions were open, and each hourly run exited in silence. The silence was
    indistinguishable from "nothing to report" — worse, it was misread as the
    cooldown branch, which actually does print.

    The tell was available the whole time: **zero out of a 5000-line file is a
    suspicious answer.** So report the denominator, not just the verdict. A count is
    only meaningful next to what was counted.
    """
    if not PQ_FILE.exists():
        return f"0 pending questions — no file at {PQ_FILE}"

    text = PQ_FILE.read_text()
    active_text = active_region(text)

    # The denominator must cover the SAME populations the numerator counts.
    # get_waiting_questions() recognizes BOTH `## ` sections and the free-form
    # `- **[label]** ...` bullets the proactive loop writes in practice, so counting
    # only sections leaves the bullet-only file — a real, supported shape — able to
    # report a trusted-looking zero in exactly the situation this function exists to
    # flag. Found in review of the first revision, with a reproduction: a file that is
    # nothing but `# Resolved` + one bullet yielded "every one is explicitly
    # resolved/answered", which is the opposite of the intended signal.
    SECTION_RE = r'^## '
    BULLET_RE = r'^\s*-\s+\*\*\['

    def _tally(s: str) -> tuple[int, int]:
        return (len(re.findall(SECTION_RE, s, flags=re.MULTILINE)),
                len(re.findall(BULLET_RE, s, flags=re.MULTILINE)))

    file_secs, file_bullets = _tally(text)
    act_secs, act_bullets = _tally(active_text)
    file_total = file_secs + file_bullets
    act_total = act_secs + act_bullets

    def _describe(secs: int, bullets: int) -> str:
        parts = []
        if secs:
            parts.append(f"{secs} '## ' section(s)")
        if bullets:
            parts.append(f"{bullets} bullet entr(ies)")
        return " + ".join(parts) if parts else "nothing"

    if file_total == 0:
        return "0 pending questions — the file holds no sections or bullets at all"

    # An empty active region is the parse-fault shape AND, permanently, a fully
    # answered file. The header tells them apart; the entries cannot.
    if act_total == 0:
        if not _active_region_lost(active_text):
            return (
                f"0 pending questions — the active region is empty and all "
                f"{_describe(file_secs, file_bullets)} sit below the archive divider"
            )
        return (
            f"0 pending questions, but {PQ_FILE.name} holds "
            f"{_describe(file_secs, file_bullets)} and has NO active-region header "
            f"at all — the '# Open' heading is gone, so there is no region left for "
            f"them to be in. That is the shape of a parse fault, not a quiet day — "
            f"check the '# Resolved' divider before trusting this zero."
        )

    return (
        f"0 pending questions — the active region holds "
        f"{_describe(act_secs, act_bullets)} (of {_describe(file_secs, file_bullets)} "
        f"in the file) and every one is explicitly resolved/answered"
    )


def main():
    force = "--force" in sys.argv
    questions = get_waiting_questions()
    if not questions:
        # Never return silently: see zero_reason.__doc__.
        print(zero_reason())
        return

    if not force and presenter_mode_active(WORKSPACE):
        print(f"(presenter-mode) {len(questions)} pending questions — suppressed")
        return

    if not force and not should_notify(notify_key(questions)):
        print(f"(cooldown) {len(questions)} pending questions — skipping notification")
        return

    count = len(questions)
    titles = [q["title"] for q in questions]

    # Cooldown is stamped only AFTER delivery returns. Stamping first meant a
    # raising delivery path still suppressed the next hour's notification — the
    # exact "claimed an outcome it never achieved" failure this script exists to
    # remove, reproduced in its own control flow.
    summary = deliver(questions, count, titles)
    write_notify_stamp(questions)  # pragma: no cover — covered as a unit; reaching here fires a real notification
    print(summary)


if __name__ == "__main__":
    main()
