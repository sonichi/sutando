#!/usr/bin/env python3
"""Daily insight generator for Sutando's behavioral flywheel.

Analyzes call logs, task history, and notes to surface one actionable pattern.
Output: results/insight-{date}.txt (voice agent can speak it).
"""

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import shared_personal_path  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
CALLS_FILE = WORKSPACE / "results" / "calls" / "calls.jsonl"
RESULTS_DIR = WORKSPACE / "results"
STATE_DIR = WORKSPACE / "state"
NOTES_DIR = Path(shared_personal_path("notes", WORKSPACE))
# The src/ dir — a known-inside-the-repo path. `git -C` here resolves the repo
# toplevel itself, so we don't walk parents to guess the checkout root (which is
# both the workspace-resolution anti-pattern the CI guard forbids and fragile).
SRC_DIR = Path(__file__).resolve().parent


def load_calls():
    if not CALLS_FILE.exists():
        return []
    calls = []
    for line in CALLS_FILE.read_text().splitlines():
        if line.strip():
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return calls


def analyze_call_timing(calls):
    """Find peak usage hours and day-of-week patterns."""
    hour_counts = Counter()
    day_counts = Counter()
    for c in calls:
        ts = c.get("start_time") or c.get("timestamp") or ""
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hour_counts[dt.hour] += 1
            day_counts[dt.strftime("%A")] += 1
        except (ValueError, AttributeError):
            pass
    return hour_counts, day_counts


def analyze_call_duration(calls):
    """Find average and outlier call durations."""
    durations = []
    for c in calls:
        dur = c.get("duration_seconds") or c.get("duration")
        if dur and isinstance(dur, (int, float)) and dur > 0:
            durations.append(dur)
    if not durations:
        return None
    avg = sum(durations) / len(durations)
    long_calls = [d for d in durations if d > avg * 2]
    return {
        "count": len(durations),
        "avg_minutes": round(avg / 60, 1),
        "longest_minutes": round(max(durations) / 60, 1),
        "long_call_pct": round(len(long_calls) / len(durations) * 100, 1),
    }


def analyze_topics(calls):
    """Extract most common topics from call summaries."""
    topics = Counter()
    for c in calls:
        summary = c.get("summary", "") or c.get("topic", "") or ""
        # Simple keyword extraction
        for word in summary.lower().split():
            word = word.strip(".,!?()[]{}:;\"'")
            if len(word) > 4 and word not in {
                "about", "their", "there", "would", "could", "should",
                "which", "where", "these", "those", "other", "after",
                "before", "between", "under", "above", "through",
            }:
                topics[word] += 1
    return topics.most_common(10)


def analyze_task_patterns():
    """Look at recent task results for patterns."""
    task_files = sorted(RESULTS_DIR.glob("task-*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    sources = Counter()
    for f in task_files[:50]:
        content = f.read_text()
        if "discord" in content.lower():
            sources["Discord"] += 1
        elif "telegram" in content.lower():
            sources["Telegram"] += 1
        elif "voice" in content.lower():
            sources["Voice"] += 1
        else:
            sources["Other"] += 1
    return sources


def analyze_note_activity():
    """Check note creation patterns."""
    notes = list(NOTES_DIR.glob("*.md"))
    recent = [n for n in notes if n.stat().st_mtime > (datetime.now().timestamp() - 7 * 86400)]
    tags = Counter()
    for n in notes:
        content = n.read_text()
        if "tags:" in content:
            tag_line = [l for l in content.split("\n") if "tags:" in l]
            if tag_line:
                for tag in tag_line[0].replace("tags:", "").replace("[", "").replace("]", "").split(","):
                    tag = tag.strip()
                    if tag:
                        tags[tag] += 1
    return {"total": len(notes), "recent_7d": len(recent), "top_tags": tags.most_common(5)}


def _git_author_identity(repo_root):
    """The local git identity (email, else name) — the "you" this insight speaks
    to. Used to count only the owner's own commits so a pull of upstream work
    doesn't inflate "you shipped N" (CR #2257). Returns "" if none is set or git
    is unavailable, in which case the caller declines to make a personal claim."""
    for key in ("user.email", "user.name"):
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_root), "config", "--get", key],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        v = r.stdout.strip()
        if r.returncode == 0 and v:
            return v
    return ""


def _own_stand_value(env=None, repo_root=None):
    """This instance's `Stand:` trailer value, or "" when it cannot be determined.

    Both Sutando instances commit under the owner's GH-mapped email (CLA requires
    it), so `--author` cannot separate them — a local-branch scan on one host
    returned 17 `Echo Act IV Mini` commits beside 16 `Echo Act IV Pro` ones, the
    peer's present only because worktrees had been created at its PR heads.

    Resolution order, all canonical — no host-name guessing:
      1. ``SUTANDO_STAND`` (explicit override)
      2. ``bash scripts/sutando-config.sh stand`` — the per-clone config key,
         which resolves with NO environment and is therefore what the scheduled
         cron actually sees (john-the-dev, #2484: the activated path exports
         neither env var, so an env-only reader silently returns "").

    An earlier revision mapped host labels containing ``mac-mini``/``macbook`` to
    this owner's Stand names. That is installation-specific policy in shared
    code: on another user's machine it would assign a foreign identity and filter
    out all of their legitimate commits. Removed.

    Returns "" when nothing resolves — and the CALLER must then decline to report,
    not count everything (see analyze_dev_activity).
    """
    env = os.environ if env is None else env
    explicit = (env.get("SUTANDO_STAND") or "").strip()
    if explicit:
        return explicit
    inside_repo = Path(repo_root) if repo_root else SRC_DIR
    try:
        top = subprocess.run(
            ["git", "-C", str(inside_repo), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if top.returncode != 0 or not top.stdout.strip():
        return ""
    script = Path(top.stdout.strip()) / "scripts" / "sutando-config.sh"
    if not script.is_file():
        return ""
    try:
        r = subprocess.run(["bash", str(script), "stand"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def analyze_dev_activity(repo_root=SRC_DIR, now=None):
    """Real code output in the last 24h, straight from git.

    Added 2026-07-21: the insight kept reducing the owner's day to "you made N
    notes" — a shallow workspace-folder metric blind to what he actually did
    (shipping commits, PRs, meetings). Notes are a side effect; commits are the
    headline. This surfaces the latter so a productive day doesn't read as
    "just notes."

    Returns ``{"commits_24h": int, "top_dirs": [(dir, n), ...]}`` or None when
    git isn't available / it's not a repo. Deliberately local + deterministic
    (git only — no ``gh`` network call) so a cron run never hangs.

    Counts only the LOCAL git identity's own commits (``--author``). Without
    this filter, a ``git pull`` that lands other contributors' work would report
    their commits as "you shipped N", the exact misleading personal metric this
    insight exists to avoid (CR #2257, qingyun-wu). If no identity resolves, we
    return None rather than attribute someone else's commits to the owner.
    """
    author = _git_author_identity(repo_root)
    if not author:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--branches", "--since=24 hours ago",
             f"--author={author}",
             "--pretty=format:C:%H\x1f%(trailers:key=Stand,valueonly)", "--name-only"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    stand = _own_stand_value(repo_root=repo_root)
    commits = 0
    dirs = Counter()
    counting = False
    seen_stands = set()
    for line in out.stdout.splitlines():
        if line.startswith("C:"):
            # "C:<sha>\x1f<Stand trailer value>" — the trailer is the ONLY thing
            # that separates this instance from its peer, because both commit
            # under the owner's GH-mapped email (see _own_stand_value).
            trailer = line.split("\x1f", 1)[1].strip() if "\x1f" in line else ""
            if trailer:
                seen_stands.add(trailer)
            counting = (not stand) or (trailer == stand)
            if counting:
                commits += 1
        elif counting and line.strip() and "/" in line:
            dirs[line.split("/", 1)[0]] += 1
    if not stand and len(seen_stands) > 1:
        # No resolvable instance AND the scan spans MORE THAN ONE — we cannot say
        # which commits are ours, and counting them all would credit the peer's
        # work as this instance's (john-the-dev, #2484). Decline.
        #
        # Deliberately narrower than "decline whenever identity is unknown": that
        # also silences every single-instance install which has never set the
        # config key, producing the other failure john named — "reports no work
        # despite real commits". Ambiguity, not ignorance, is what makes a count
        # unsafe.
        return None
    if commits == 0:
        return None
    return {"commits_24h": commits, "top_dirs": dirs.most_common(3), "stand": stand}


def dev_activity_insight(dev):
    """Render the dev-activity dict into one headline sentence, or None."""
    if not dev or dev.get("commits_24h", 0) <= 0:
        return None
    n = dev["commits_24h"]
    where = ", ".join(f"{d}/" for d, _ in dev["top_dirs"]) or "the codebase"
    plural = "s" if n != 1 else ""
    stand = (dev.get("stand") or "").strip()
    subject = f"Sutando's {stand} instance" if stand else "Sutando"
    return (
        f"{subject} shipped {n} commit{plural} in the last 24h, mostly in {where}. "
        f"That's the real headline — steady build velocity."
    )


def generate_insight():
    # Real code output is the highest-signal insight — surface it first so a
    # productive day never reads as "you just made some notes" (owner 2026-07-21).
    dev_line = dev_activity_insight(analyze_dev_activity())
    if dev_line:
        return dev_line

    calls = load_calls()
    insights = []

    if calls:
        hour_counts, day_counts = analyze_call_timing(calls)
        dur_stats = analyze_call_duration(calls)

        if hour_counts:
            peak_hour = hour_counts.most_common(1)[0]
            quiet_hours = [h for h in range(8, 22) if hour_counts.get(h, 0) == 0]
            if quiet_hours:
                insights.append(
                    f"You've made {len(calls)} calls total. Peak hour: {peak_hour[0]}:00 "
                    f"({peak_hour[1]} calls). Hours with zero calls: {', '.join(f'{h}:00' for h in quiet_hours[:3])}. "
                    f"Consider scheduling deep work during those quiet windows."
                )

        if day_counts:
            busiest = day_counts.most_common(1)[0]
            quietest = day_counts.most_common()[-1]
            if busiest[1] > quietest[1] * 2:
                insights.append(
                    f"{busiest[0]}s are your busiest day ({busiest[1]} calls) — "
                    f"{quietest[0]}s are quietest ({quietest[1]}). "
                    f"You might want to protect {busiest[0]} mornings for focused work."
                )

        if dur_stats and dur_stats["long_call_pct"] > 20:
            insights.append(
                f"{dur_stats['long_call_pct']}% of your calls run longer than average "
                f"({dur_stats['avg_minutes']} min avg). Longest: {dur_stats['longest_minutes']} min. "
                f"Setting a timer or agenda could reclaim significant time."
            )

    note_stats = analyze_note_activity()
    if note_stats["recent_7d"] > 5:
        insights.append(
            f"You've created {note_stats['recent_7d']} notes in the last 7 days "
            f"({note_stats['total']} total). Top tags: {', '.join(t[0] for t in note_stats['top_tags'][:3])}. "
            f"Your notes system is active and growing."
        )
    elif note_stats["recent_7d"] == 0 and note_stats["total"] > 10:
        insights.append(
            f"No new notes in the last 7 days (you have {note_stats['total']} total). "
            f"You were actively noting ideas before — might be worth capturing what you're learning this week."
        )

    task_sources = analyze_task_patterns()
    if task_sources:
        top_source = task_sources.most_common(1)[0]
        insights.append(
            f"Most tasks come through {top_source[0]} ({top_source[1]} recent). "
            f"Channel mix: {dict(task_sources)}."
        )

    if not insights:
        insights.append("Not enough data yet to generate behavioral insights. Keep using Sutando — patterns will emerge.")

    # Pick the most interesting one (longest = most specific)
    best = max(insights, key=len)
    return best


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    output_path = RESULTS_DIR / f"insight-{today}.txt"
    # Sentinel survives discord-bridge's `dm-fallback` unlink of the
    # results file, so repeat invocations (morning-briefing, cron, manual
    # test) on the same day don't regenerate + re-DM the insight.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = STATE_DIR / f"daily-insight-{today}.sentinel"

    if sentinel.exists():
        cached = sentinel.read_text()
        print(f"Insight already generated today (sentinel: {sentinel})")
        print(cached)
        return

    insight = generate_insight()
    output_path.write_text(insight)
    sentinel.write_text(insight)
    print(f"Daily insight → {output_path}")
    print(insight)

    # Anonymous, opt-out product telemetry: one bucketed event when this feature
    # actually runs (not on the cached early return). Never content/PII.
    try:  # pragma: no cover — bounded flush; logic tested in tests/telemetry.test.py
        from telemetry import feature_used  # sibling module (src/ on sys.path)

        feature_used("daily_insight", flush=True)
    except Exception:  # pragma: no cover — telemetry must never break the feature
        pass


if __name__ == "__main__":
    main()
