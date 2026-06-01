# submit-use-case

OSS-facing channel to share a Sutando use case with the community.

Opens a GitHub issue on [`sonichi/sutando`](https://github.com/sonichi/sutando)
(label `use-case-submission`) and a PR adding
`docs/community-use-cases/<slug>.md`. Both happen by default; flags `--issue-only`
and `--pr-only` opt out of one or the other.

This is the public counterpart to the internal `add-use-case` skill, which
targets the private `AG2Platform/agent-universe` rendering repo. OSS
contributors don't have write access there, so they submit here instead and
a maintainer promotes accepted submissions over to the rendering repo.

## Quick start

```bash
python3 scripts/submit_use_case.py \
  --title "Your AI plays piano while you sleep" \
  --summary "Sutando learned your favorite Chopin nocturnes and a soft variation routine for the small hours." \
  --bullets "Trained on a folder of midi nocturnes" \
  --bullets "Cuts off the moment your sleep-tracker flags REM" \
  --video /path/to/clip.mp4 \
  --x-url https://x.com/yourhandle/status/123456789 \
  --contact "you@example.com or @yourhandle"
```

Returns the issue URL and PR URL on stdout (last two lines).

## Title framing

Same outcome-first checker as `add-use-case`. Titles must say what the USER
achieves, not what the AI does. Capability-listy titles ("Ask your AI to
...", "Sutando can ...", "Send a tweet by voice") are rejected with reframe
suggestions and a non-zero exit. Gold-standard outcome titles:

- "Run your business hands-free"
- "Catch and fix your own bugs"
- "Your AI books your dinner — and talks to their AI"

## Dry run

```bash
python3 scripts/submit_use_case.py --dry-run --title "..." --summary "..." \
  --bullets "..."
```

Prints the rendered issue body + the rendered
`docs/community-use-cases/<slug>.md` to stdout. No clone, no gh calls.

## Idempotency

- Branch `community-use-case/<slug>` checked on `origin` before pushing.
- Open issue with the same `title` aborts with a pointer.

## Identity / CLA

`sonichi/sutando` has CLA-Assistant enabled. Commits must be signed under
the contributor's own email — that's how the CLA channel works.

- **OSS contributors** (default): the script does NOT touch your `git
  config`. The CLA gets signed under your real identity. Just make sure your
  `gh` auth and `git config user.email` match the email you want on file.
- **Chi's fleet** (auto-detected via `/Users/wangchi/.sutando/workspace/` or
  env `SUTANDO_FLEET_OWNER=chi`): the script sets repo-local
  `user.email=4250911+sonichi@users.noreply.github.com` and `user.name=Chi
  Wang` inside the fresh clone so internal demo submissions don't need an
  extra config step.

## Dependencies

- `git`, `gh` (authenticated to GitHub as the submitter)
- Python ≥ 3.9 (stdlib only)
