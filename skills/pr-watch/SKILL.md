---
name: pr-watch
description: "Event-driven GitHub PR watching. Use when the owner wants to be kept on top of pull requests: re-review a person's PRs whenever they change, react when a review is requested from the owner, or note merges, closes and submitted reviews, without polling and without a Monitor that expires after 30 minutes. A Claude Code mod runs `gh webhook forward` for each watched repo and turns each relevant event into a new turn of this session."
user-invocable: true
---

# pr-watch

Watches GitHub pull requests by event, not by polling. A Claude Code mod (a plugin of function
hooks) starts `pr_watch.py run` when the session starts; the runner keeps one
`gh webhook forward` per watched repo and prints one line per relevant event. The mod submits
each line as a new prompt (`$.prompt.submit`), so every event becomes its own turn once the
session is idle. Nothing polls GitHub, and nothing expires the way a `Monitor` does at 30 minutes.

## When to use it

- "Re-review Alice's PRs whenever she pushes", "tell me when Bob's PR merges".
- "Ping me when someone requests my review on acme/widgets."
- Any standing PR watch that should survive past one Monitor window.

Not for one-off checks of a single PR: read it with `gh pr view` instead.

## Events forwarded

Per watched repo (`config.json`), filtered by `pr_watch.py`:

| Event | Forwarded when | Line |
|---|---|---|
| `pull_request` `opened`, `synchronize`, `reopened`, `ready_for_review`, `closed` | the PR author is a watched `--author` (`*` = anyone) | `PR_EVENT o/r#N <action> author=… head=<sha8> merged=true\|false` |
| `pull_request` `review_requested` | the requested reviewer is one of the `--me` logins, on any PR in the repo | `PR_EVENT o/r#N review_requested reviewer=… author=… head=…` |
| `pull_request_review` `submitted` | on a watched author's PR | `PR_EVENT o/r#N review_submitted by=… state=approved\|changes_requested\|commented author=… head=…` |

Only logins, numbers, states and short shas reach the prompt; titles and bodies never do, so a
PR's text cannot steer the session. The agent reads the PR itself when it acts.

## Requirements

- `gh`, signed in, with **admin** on every watched repo (creating a webhook needs it).
- The `cli/gh-webhook` extension. Installing it is a download, so **ask the owner first**, then
  `gh extension install cli/gh-webhook`. Without it the runner prints a `PR_WATCH_NOTE` and exits.
- `python3` (standard library only).

## Setup

```bash
S=skills/pr-watch/scripts/pr_watch.py
python3 $S add --repo acme/widgets --author alice --author bob --me owner-login
python3 $S install-mod --mods-dir "<the session's mods folder>"
```

`install-mod` writes the mod into `<mods-dir>/pr-watch/` and copies the runner to
`<workspace>/state/pr-watch/pr_watch.py`, so the forwarder keeps working when the engine
checkout changes branch or updates. Re-run it after updating this skill.

Loading the mod is the owner's one step: **enable hot reload of the mods folder in the
session's terminal** (one click), or start the session with `claude --plugin-dir <mods-dir>/pr-watch`.
Never enable it for them.

Other commands:

```bash
python3 $S list                    # the watch config
python3 $S remove --repo acme/widgets
python3 $S status                  # runner, forwarders, recorded hook ids
python3 $S cleanup                 # delete the hook ids this skill recorded (refused while a runner is active; --force)
```

`add` and `remove` signal a running runner (SIGHUP), which starts or stops that repo's
forwarder without a reload.

## Configuration

`<workspace>/state/pr-watch/config.json`:

```json
{
  "watches": [{"repo": "acme/widgets", "authors": ["alice"], "me": ["owner-login"]}],
  "prompt": "[pr-watch] {event}\nStanding ask from the owner: ..."
}
```

`prompt` is the template each event's prompt is built from; `{event}` is replaced by the line.
The default tells the agent to re-review at the new head, review what is requested from the
owner, note merges and closes, and tell the owner only when something needs them. The mod
re-reads it on every event, so an edit takes effect at once.

## Limits, and how they are handled

1. **One forwarder hook per repo.** GitHub refuses a second with `HTTP 422 Hook already exists`.
   Before starting, the runner lists the repo's forwarder hooks (`name: cli`, url
   `https://webhook-forwarder.github.com/hook`); if one exists that it did not create, it prints a
   `PR_WATCH_NOTE`, does not start, and retries every 5 minutes. Another session or person
   forwarding the same repo therefore blocks this watch until they stop.
2. **A killed forwarder can leave its hook behind.** The runner records the ids of the hooks its
   forwarders create in `state/pr-watch/hooks.json`, and deletes exactly those ids when a forwarder
   restarts, when the runner stops, and on its next start. It never deletes a hook it did not
   record. A leftover it never recorded (a crash in the first seconds) shows up as point 1; delete
   it by hand after checking it is not someone else's.
3. **Runner killed outright.** Its `gh` children survive re-parented. Their pids are kept in
   `state/pr-watch/forwarders.json`, and the next start stops them before starting new ones.
4. **Mod-written files reload the mod.** All state and the runner copy live under
   `<workspace>/state/pr-watch/`, never in the mod folder.
5. **Lifetime.** The runner lives as long as the mod's module: a reload or session end kills it,
   and the next `session.start` respawns it. One runner per state dir (a lock); a second exits.
   If the runner exits on its own it is restarted after 30 s, except for a missing extension or
   a held lock.
6. **Events while no session runs are lost.** `gh webhook forward` does not replay; after a gap,
   check the watched PRs once by hand.

## Costs

- Each forwarded event is a full agent turn. A busy author pushing often means many turns; keep
  `--author` lists tight. Identical lines within 60 s are dropped by the mod.
- One `gh webhook forward` process and a few GitHub API calls per repo at (re)start; nothing
  between events.
- `gh webhook forward` is a development tool from GitHub; its hooks are visible to every repo
  admin under Settings → Webhooks while the watch runs.

## Files

- `scripts/pr_watch.py`: the CLI and the runner.
- `mod/`: the Claude Code mod (`hooks/register.ts`; `hooks/paths.ts` is written per install).
  Test it with `claude plugin validate skills/pr-watch/mod` and `claude plugin test skills/pr-watch/mod`.
- Tests: `tests/skills/pr-watch/pr_watch.test.py`, `mod/tests/watch.test.ts`.
