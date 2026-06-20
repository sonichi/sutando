---
name: submit-use-case
description: "OSS-facing channel to share a Sutando use case with the community. Opens a labeled GitHub issue on sonichi/sutando and (CLA-gated) a PR adding docs/community-use-cases/<slug>.md. Validates outcome-framed titles with the same checker as add-use-case. Default: both; flags --issue-only / --pr-only opt out."
user-invocable: true
---

# submit-use-case

OSS counterpart to `add-use-case`. `add-use-case` targets the private
`AG2Platform/agent-universe` repo (rendered on sutando.ai) and is for the
internal fleet. `submit-use-case` targets the public `sonichi/sutando` repo
so external contributors can share a use case without needing access to the
private rendering repo.

## Usage

```bash
python3 "$SKILL_DIR/scripts/submit_use_case.py" \
    --title "Your AI plays piano while you sleep" \
    --summary "Outcome-first summary that appears on tile + detail page." \
    --bullets "bullet1" --bullets "bullet2" \
    [--video /abs/path/to/clip.mp4] \
    [--media-url https://example.com/clip.jpg] \
    [--youtube-id ABCDEFG1234] \
    [--x-url https://x.com/...] \
    [--linkedin-url https://www.linkedin.com/...] \
    [--contact "you@example.com or @yourhandle"] \
    [--issue-only | --pr-only] \
    [--dry-run]
```

`--dry-run` runs the framing check and prints both the rendered issue body
and the rendered `docs/community-use-cases/<slug>.md` file to stdout. No clone, no
gh calls.

## What the skill does

1. **Validate title is outcome-framed.** Reuses `REJECT_PATTERNS` + `validate_title` +
   `suggest_reframes` from `add-use-case` verbatim. Capability-listy titles
   ("Ask your AI to ...", "Sutando can ...", "Send a ...") are rejected with
   deterministic reframe suggestions. Same rules, same voice.
2. **Derive slug.** `slugify(title)` — lowercased, non-alphanum→`-`, trimmed to 60 chars.
3. **Idempotency checks** (skipped in `--dry-run`):
   - Does a branch `community-use-case/<slug>` already exist on `sonichi/sutando`?
     If yes, abort with a pointer to the existing PR.
   - Is there an open issue on `sonichi/sutando` with the same `title`? If yes,
     abort with the existing issue URL.
4. **Open an issue** (unless `--pr-only`). `gh issue create --repo sonichi/sutando`
   with label `use-case-submission`. Body has structured sections: Title,
   Summary, Bullets, Links (video / youtube / x / linkedin), Contact, and a
   forward-ref to the PR if one is opened in the same invocation.
5. **Open a PR** (unless `--issue-only`).
   - Fresh clone: `gh repo clone sonichi/sutando /tmp/sutando-submit-use-case-{ts}/`
   - Branch: `community-use-case/<slug>` from `main`
   - File: `docs/community-use-cases/<slug>.md` — YAML frontmatter mirroring the
     agent-universe `UseCase` shape so the sync script can later promote it.
   - Identity: the script **never overrides** git config — the commit is
     attributed to the runner's existing `user.name` / `user.email`, so the CLA
     signs under whoever they already are (that's the whole point of CLA-Assistant
     signing). It prints the identity before committing and aborts if it's unset,
     so attribution can't silently go out wrong.
   - Commit, push, `gh pr create`. PR body cross-links the issue if one was opened.
6. **Return** both URLs to stdout (issue URL, then PR URL, last two lines).

## File schema (`docs/community-use-cases/<slug>.md`)

```yaml
---
slug: <derived>
title: <user title>
summary: <user summary>
videoUrl: <if --video>            # absolute or hosted; raw path uploaded to issue, not PR
youtubeId: <if --youtube-id>
xUrl: <if --x-url>
linkedinUrl: <if --linkedin-url>
thumbnail: /use-cases/<slug>.jpg
contact: <if --contact>
submitted_at: <ISO UTC timestamp>
---

<longDescription stitched from --bullets, or summary if no bullets>
```

The frontmatter intentionally mirrors `AG2Platform/agent-universe`'s
`lib/use-cases.ts` `UseCase` TypeScript type so a later sync script can read
this YAML and emit the literal entry the rendering repo expects. The
submitter is **not** asked to upload the thumbnail in the PR — it stays as a
pending path that the maintainer fills in when promoting.

## Issue label

`use-case-submission` (the maintainer monitors this label to triage incoming
community submissions).

## Idempotency

- Branch name `community-use-case/<slug>` is checked on `origin` before pushing.
- Existing issue with the same `title` (case-insensitive, open state) aborts.
- `--dry-run` skips all remote checks and only prints rendered output.

## Why we never override git identity

CLA-Assistant signs the contributor under the email of the commit author.
That MUST be the runner's own email — overwriting it would wrongly attribute
the PR and sign the CLA under the wrong identity. So the script never sets
`user.email` / `user.name`: it commits under whatever the runner already has
configured (the maintainer's machine already carries the project identity in
its global git config; contributors carry their own). It surfaces the identity
before committing and aborts if unset, rather than guessing or silently using
the wrong author.

## Dependencies

- `git`, `gh` (authenticated to GitHub as the submitter)
- Python ≥ 3.9 (stdlib only — no third-party libs)
