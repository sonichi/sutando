#!/usr/bin/env python3
"""Submit a use-case to the public sonichi/sutando repo (OSS channel).

Pipeline: validate title framing -> idempotency checks -> open labeled issue
on sonichi/sutando -> fresh clone -> branch -> write
docs/community-use-cases/<slug>.md -> commit (CLA identity rules) -> push ->
PR. Both halves can be
opted out via --issue-only / --pr-only.

Reuses REJECT_PATTERNS / validate_title / suggest_reframes from the internal
add-use-case skill verbatim (copied here so this skill has no cross-skill
import dependency).

Usage:
    submit_use_case.py --title T --summary S [--bullets B1 ...] \
        [--video PATH] [--media-url URL] [--youtube-id ID] \
        [--x-url URL] [--linkedin-url URL] [--contact STR] \
        [--issue-only | --pr-only] [--dry-run]
"""
from __future__ import annotations

import argparse, datetime, json, re, shutil, subprocess, sys, time
from pathlib import Path

REPO = "sonichi/sutando"
REPO_URL = f"https://github.com/{REPO}.git"
ISSUE_LABEL = "use-case-submission"

# Capability-framed patterns to reject (verbatim from add-use-case).
# Outcome-framed titles state what the USER achieves, not what the AI does.
REJECT_PATTERNS = [
    r"^ask\s+(your\s+)?ai\b",
    r"^sutando\s+(can|will|does)\b",
    r"^send\s+(a|an|your)\b",
    r"^read\s+(a|an|your)\b",
    r"^open\s+(a|an|your)\b",
    r"\b(ai|agent|assistant)\s+(that\s+)?(can|will)\s+",  # "AI that can X"
]


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:60]


def run(cmd, **kw):
    kw.setdefault("check", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


# --- framing check (verbatim from add-use-case) -----------------------------
def validate_title(title: str) -> tuple[bool, str]:
    """Return (ok, reason). Borderline cases pass with a warn note."""
    t = title.strip()
    if len(t) < 6 or len(t) > 80:
        return False, f"title length {len(t)} chars (want 6-80)"
    if t.endswith("."):
        return False, "drop trailing period; titles are tile labels not sentences"
    for pat in REJECT_PATTERNS:
        if re.search(pat, t, re.I):
            return False, f"capability-framed (matched /{pat}/) — reframe as a user outcome"
    return True, "outcome-framed"


def suggest_reframes(title: str) -> list[str]:
    """Cheap deterministic suggestions. Caller can invoke a subagent for richer ones."""
    t = title.strip().rstrip(".")
    return [
        f"Have your AI {t.lower()}",
        f"{t} — hands-free",
        "Outcome-framed example: \"Run your business hands-free\"",
    ]


# --- identity (read-only; never overridden) ---------------------------------
def committer_identity(clone_dir: str | None = None) -> tuple[str, str]:
    """Return the (name, email) git will attribute the commit to — the runner's
    EXISTING git config, which this script never overrides.

    No owner-detection: the maintainer's own machine already has the CLA-signed
    identity in its global git config, and OSS contributors MUST commit under
    their own email so they sign their own CLA (the whole point of the
    CLA-Assistant channel). The script just surfaces the identity (and fails loud
    if it's unset) so attribution can't silently go out wrong.
    """
    base = ["git"] + (["-C", clone_dir] if clone_dir else [])
    def _get(key: str) -> str:
        r = subprocess.run(base + ["config", key], capture_output=True, text=True)
        return r.stdout.strip()
    return _get("user.name"), _get("user.email")


# --- idempotency probes -----------------------------------------------------
def check_remote_branch_exists(branch: str) -> bool:
    r = subprocess.run(
        ["gh", "api", f"repos/{REPO}/branches/{branch}", "--silent"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def existing_pr_url(slug: str) -> str | None:
    branch = f"community-use-case/{slug}"
    r = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO,
         "--head", branch, "--json", "url", "--state", "all"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        rows = json.loads(r.stdout or "[]")
        return rows[0]["url"] if rows else None
    except Exception:
        return None


def existing_issue_url(title: str) -> str | None:
    """Return URL of an open issue on sonichi/sutando whose title equals `title`
    (case-insensitive). Uses `gh issue list --search` then filters in-process
    because gh's search treats the query as full-text, not exact title."""
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO,
         "--state", "open", "--label", ISSUE_LABEL,
         "--json", "title,url", "--limit", "200"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return None
    t = title.strip().lower()
    for row in rows:
        if (row.get("title") or "").strip().lower() == t:
            return row.get("url")
    return None


# --- content rendering ------------------------------------------------------
def render_long_description(summary: str, bullets: list[str]) -> str:
    """Deterministic stitched paragraph: summary followed by joined bullets.

    Bullets always extend the summary when present (no length-comparison fallback
    that silently dropped short bullets in earlier revisions).
    """
    if not bullets:
        return summary
    body = " ".join(b.rstrip(".") + "." for b in bullets)
    return f"{summary} {body}"


def _yaml_escape(s: str) -> str:
    # Use double-quoted YAML scalar; escape backslash + double-quote.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render_pr_file(
    *, slug: str, title: str, summary: str, long_desc: str,
    video_url: str | None, youtube_id: str | None,
    x_url: str | None, linkedin_url: str | None,
    contact: str | None, submitted_at: str,
) -> str:
    """Render the docs/community-use-cases/<slug>.md file body."""
    lines = ["---"]
    lines.append(f'slug: "{_yaml_escape(slug)}"')
    lines.append(f'title: "{_yaml_escape(title)}"')
    lines.append(f'summary: "{_yaml_escape(summary)}"')
    if video_url:
        lines.append(f'videoUrl: "{_yaml_escape(video_url)}"')
    if youtube_id:
        lines.append(f'youtubeId: "{_yaml_escape(youtube_id)}"')
    if x_url:
        lines.append(f'xUrl: "{_yaml_escape(x_url)}"')
    if linkedin_url:
        lines.append(f'linkedinUrl: "{_yaml_escape(linkedin_url)}"')
    lines.append(f'thumbnail: "/use-cases/{_yaml_escape(slug)}.jpg"')
    if contact:
        lines.append(f'contact: "{_yaml_escape(contact)}"')
    lines.append(f'submitted_at: "{submitted_at}"')
    lines.append("---")
    lines.append("")
    lines.append(long_desc)
    lines.append("")
    return "\n".join(lines)


def render_issue_body(
    *, slug: str, title: str, summary: str, bullets: list[str],
    video_path: str | None, media_url: str | None, youtube_id: str | None,
    x_url: str | None, linkedin_url: str | None,
    contact: str | None, pr_branch: str | None,
) -> str:
    lines = [f"## Use case: {title}", "", "### Summary", summary, ""]
    if bullets:
        lines.append("### What happens")
        for b in bullets:
            lines.append(f"- {b}")
        lines.append("")
    links = []
    if video_path:
        links.append(f"- Video (local path on submitter's machine): `{video_path}`")
    if media_url:
        links.append(f"- Media: {media_url}")
    if youtube_id:
        links.append(f"- YouTube: https://youtu.be/{youtube_id}")
    if x_url:
        links.append(f"- X: {x_url}")
    if linkedin_url:
        links.append(f"- LinkedIn: {linkedin_url}")
    if links:
        lines.append("### Links")
        lines.extend(links)
        lines.append("")
    if contact:
        lines.append("### Contact")
        lines.append(contact)
        lines.append("")
    lines.append("### Meta")
    lines.append(f"- slug: `{slug}`")
    lines.append(f"- label: `{ISSUE_LABEL}`")
    if pr_branch:
        lines.append(f"- companion PR branch: `{pr_branch}`")
    lines.append("")
    lines.append("### Framing check")
    lines.append(
        "Title passed the outcome-framing gate (rejects capability-listy titles "
        "like \"Ask your AI to ...\", \"Sutando can ...\"). Matches the voice of "
        "existing entries (\"Run your business hands-free\", \"Catch and fix your own bugs\")."
    )
    return "\n".join(lines)


# --- main pipeline ----------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--bullets", action="append", default=[])
    ap.add_argument("--video", type=Path,
                    help="Local path; referenced in the issue body, NOT uploaded.")
    ap.add_argument("--media-url",
                    help="Hosted media URL (image or video link). Recorded in frontmatter as videoUrl for compat with the AG2Platform UseCase schema; rendered as a link in the issue body.")
    ap.add_argument("--youtube-id")
    ap.add_argument("--x-url")
    ap.add_argument("--linkedin-url")
    ap.add_argument("--contact",
                    help='Email or handle the maintainer can reach you at.')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--issue-only", action="store_true")
    mode.add_argument("--pr-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.video and not args.video.exists():
        die(f"--video path does not exist: {args.video}")

    ok, reason = validate_title(args.title)
    if not ok:
        print(f"TITLE REJECTED: {reason}", file=sys.stderr)
        print("Suggestions (outcome-framed reframes):", file=sys.stderr)
        for s in suggest_reframes(args.title):
            print(f"  - {s}", file=sys.stderr)
        sys.exit(2)
    print(f"title framing: OK ({reason})")

    slug = slugify(args.title)
    if not slug:
        die("slug derived from title is empty")
    print(f"slug: {slug}")

    submitted_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    branch = f"community-use-case/{slug}"
    do_issue = not args.pr_only
    do_pr = not args.issue_only

    long_desc = render_long_description(args.summary, args.bullets)
    pr_file_text = render_pr_file(
        slug=slug, title=args.title, summary=args.summary, long_desc=long_desc,
        video_url=args.media_url, youtube_id=args.youtube_id,
        x_url=args.x_url, linkedin_url=args.linkedin_url,
        contact=args.contact, submitted_at=submitted_at,
    )
    issue_body = render_issue_body(
        slug=slug, title=args.title, summary=args.summary, bullets=args.bullets,
        video_path=str(args.video) if args.video else None,
        media_url=args.media_url, youtube_id=args.youtube_id,
        x_url=args.x_url, linkedin_url=args.linkedin_url,
        contact=args.contact, pr_branch=branch if do_pr else None,
    )

    if args.dry_run:
        print("--- ISSUE BODY ---")
        print(issue_body)
        print("--- END ISSUE BODY ---")
        print(f"--- PR FILE (docs/community-use-cases/{slug}.md) ---")
        print(pr_file_text)
        print("--- END PR FILE ---")
        _nm, _em = committer_identity()
        print(f"identity: would commit as {_nm or '(unset)'} <{_em or '(unset)'}> — your existing git config, never overridden. Ensure it's the identity you want on the CLA.")
        print(f"DRY RUN — staged in memory only, no clone/issue/PR")
        return

    # Idempotency probes (live only).
    if do_pr and check_remote_branch_exists(branch):
        url = existing_pr_url(slug)
        die(f"existing-branch: {branch} already on origin"
            + (f" (PR: {url})" if url else ""))
    if do_issue:
        existing = existing_issue_url(args.title)
        if existing:
            die(f"existing-issue: open issue with same title already at {existing}")

    issue_url = None
    if do_issue:
        # Open the issue first so the PR body can cross-link to it.
        issue_create = subprocess.run(
            ["gh", "issue", "create", "--repo", REPO,
             "--title", args.title, "--label", ISSUE_LABEL,
             "--body", issue_body],
            capture_output=True, text=True, check=True,
        )
        issue_url = issue_create.stdout.strip().splitlines()[-1]
        print(f"issue: {issue_url}")

    pr_url = None
    if do_pr:
        ts = int(time.time())
        clone_dir = Path(f"/tmp/sutando-submit-use-case-{ts}")
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        print(f"cloning into {clone_dir} ...")
        run(["gh", "repo", "clone", REPO, str(clone_dir), "--", "--depth", "1"])

        # Identity is NEVER overridden — the commit is attributed to the runner's
        # existing git config. On the maintainer's machine that is already the
        # CLA-signed identity; OSS contributors commit under their own email so
        # they sign their own CLA. Surface it (so attribution can't silently go
        # out wrong) and fail loud rather than letting git commit with no author.
        _nm, _em = committer_identity(str(clone_dir))
        if not _nm or not _em:
            die("git user.name / user.email is not set — configure git before "
                "submitting (the commit's author is what signs the CLA).")
        print(f"identity: committing as {_nm} <{_em}> (your existing git config; not overridden)")

        run(["git", "-C", str(clone_dir), "checkout", "-b", branch])

        target_dir = clone_dir / "docs" / "community-use-cases"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{slug}.md"
        if target_file.exists():
            die(f"existing-slug: docs/community-use-cases/{slug}.md already on main")
        target_file.write_text(pr_file_text)
        print(f"wrote {target_file.relative_to(clone_dir)}")

        run(["git", "-C", str(clone_dir), "add", f"docs/community-use-cases/{slug}.md"])
        run(["git", "-C", str(clone_dir), "commit", "-m",
             f"feat(community-use-cases): submit {slug}"])
        run(["git", "-C", str(clone_dir), "push", "-u", "origin", branch])

        pr_body_lines = [
            f"## Community use-case: {args.title}",
            "",
            args.summary,
            "",
            f"**Slug:** `{slug}`",
        ]
        if issue_url:
            pr_body_lines.append(f"**Companion issue:** {issue_url}")
        pr_body_lines += [
            "",
            "### Framing check",
            "Title passed the outcome-framing gate (rejects capability-listy "
            "titles like \"Ask your AI to ...\", \"Sutando can ...\"). Matches "
            "the voice of existing entries (\"Run your business hands-free\", "
            "\"Catch and fix your own bugs\").",
            "",
            "### Notes for the maintainer",
            "- Frontmatter mirrors the `UseCase` TypeScript type used by "
            "`AG2Platform/agent-universe/lib/use-cases.ts`, so a sync script "
            "can read this YAML and emit the literal entry the rendering repo expects.",
            "- Thumbnail path is a placeholder (`/use-cases/<slug>.jpg`) — the "
            "submitter is not asked to upload the asset here; fill it in at "
            "promotion time.",
        ]
        pr = subprocess.run(
            ["gh", "pr", "create", "--repo", REPO,
             "--base", "main", "--head", branch,
             "--title", f"feat(community-use-cases): submit {slug}",
             "--body", "\n".join(pr_body_lines)],
            capture_output=True, text=True, check=True,
        )
        pr_url = pr.stdout.strip().splitlines()[-1]
        print(f"pr: {pr_url}")

    # Final two lines: machine-readable.
    if issue_url:
        print(issue_url)
    if pr_url:
        print(pr_url)


if __name__ == "__main__":
    main()
