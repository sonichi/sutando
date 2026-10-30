---
name: pr-watch
description: "Event-driven GitHub PR watching. Use when the owner wants to be kept on top of pull requests: re-review a person's PRs whenever they change, follow up on any PR the owner has already reviewed, react when a review is requested from the owner, or note merges, closes and submitted or dismissed reviews, without polling and without a Monitor that expires after 30 minutes. A Claude Code mod runs `gh webhook forward` for each watched repo and turns each relevant event into a new turn of this session. Optionally keeps an HTML tracker page of the watched PRs in a configured Room Commons room."
user-invocable: true
---

# pr-watch

Watches GitHub pull requests by event, not by polling. A Claude Code mod (a plugin of function
hooks) starts `pr_watch.py run` when the session starts; the runner keeps one
`gh webhook forward` per watched repo and prints one line per relevant event. Nothing polls
GitHub, and nothing expires the way a `Monitor` does at 30 minutes.

How an event reaches the session depends on whether a turn is running:

- **Idle:** the mod submits the event as a new prompt (`$.prompt.submit`), which starts a turn.
- **Turn running:** a submitted prompt would only wait for the turn to end, so the mod holds the
  event and attaches it to the next tool result of the main loop (the tool call's `context`,
  which the model reads right after that tool's output). Events still held when the turn ends
  are submitted together as one prompt. Subagents' tool calls never carry them.

## When to use it

- "Re-review Alice's PRs whenever she pushes", "tell me when Bob's PR merges".
- "Ping me when someone requests my review on acme/widgets."
- "Keep an eye on every PR I've reviewed there, whoever wrote it."
- Any standing PR watch that should survive past one Monitor window.

Not for one-off checks of a single PR: read it with `gh pr view` instead.

## Events forwarded

Per watched repo (`config.json`), filtered by `pr_watch.py`:

| Event | Forwarded when | Line |
|---|---|---|
| `pull_request` `opened`, `synchronize`, `reopened`, `ready_for_review`, `closed` | the PR author is a watched `--author` (`*` = anyone) | `PR_EVENT o/r#N <action> author=… head=<sha8> merged=true\|false reason=author` |
| `pull_request` `synchronize`, `reopened`, `ready_for_review`, `closed` | the watch has `--reviewed` and one of the `--me` logins has submitted a review on the PR, whoever wrote it | `PR_EVENT o/r#N <action> author=… head=<sha8> merged=… reason=reviewed` |
| `pull_request` `review_requested` | the requested reviewer is one of the `--me` logins, on any PR in the repo | `PR_EVENT o/r#N review_requested reviewer=… author=… head=…` |
| `pull_request_review` `submitted` | on a watched author's PR, by someone other than the `--me` logins (the owner's own review echoing back is dropped, though it still marks the PR reviewed) | `PR_EVENT o/r#N review_submitted by=… state=approved\|changes_requested\|commented author=… head=…` |
| `pull_request_review` `dismissed` | the dismissed review is by one of the `--me` logins, on any PR in the repo | `PR_EVENT o/r#N review_dismissed reviewer=… author=… head=…` |

An event matching both the author rule and the reviewed rule yields one line, `reason=author`.
The webhook payload does not list a PR's reviewers, so the reviewed rule asks
`GET repos/{repo}/pulls/{n}/reviews` (see Costs). When that call fails or times out (10 s), the
event is forwarded anyway with `reason=reviewed reviewed=unknown`, never dropped.

Only logins, numbers, states and short shas reach the prompt; titles and bodies never do, so a
PR's text cannot steer the session. The agent reads the PR itself when it acts. (The runner does
store each PR's title in `tracker.json` for the tracker page's About column; see Tracker.)

## Requirements

- `gh`, signed in, with **admin** on every watched repo (creating a webhook needs it).
- The `cli/gh-webhook` extension. Installing it is a download, so **ask the owner first**, then
  `gh extension install cli/gh-webhook`. Without it the runner prints a `PR_WATCH_NOTE` and exits.
- `python3` (standard library only).

## Setup

```bash
S=skills/pr-watch/scripts/pr_watch.py
python3 $S add --repo acme/widgets --author alice --author bob --me owner-login
python3 $S add --repo acme/gadgets --me owner-login --reviewed   # every PR owner-login reviewed
python3 $S install-mod --mods-dir "<the session's mods folder>"
```

`install-mod` writes the mod into `<mods-dir>/pr-watch/` and copies the runner (and
`manifest.json`, for its settings) to `<workspace>/state/pr-watch/`, so the forwarder keeps working when the engine
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
python3 $S tracker-kind --kind html-<id> --room '!room:server'   # the page a session publish wrote to
```

`add` and `remove` signal a running runner (SIGHUP), which starts or stops that repo's
forwarder without a reload.

## Configuration

The watches live in `<workspace>/state/pr-watch/config.json`, written by `add` and `remove`:

```json
{
  "watches": [{"repo": "acme/widgets", "authors": ["alice"], "me": ["owner-login"], "reviewed": true}],
  "settings": {"PR_WATCH_TRACKER_ROOM": "!room:server"}
}
```

`reviewed` (set by `add --reviewed`) needs at least one `me` login other than `*`.

The skill's settings are declared, with their defaults, in the `config` block of
`manifest.json` (see `skills/MANIFEST.md`, "Config-only manifests"):

| Key | Default | Meaning |
|---|---|---|
| `PR_WATCH_PROMPT` | the standing ask below | the template each event's prompt is built from: `{event}` becomes the event line(s), `{track}` the tracker hint (empty when no tracker room is set) |
| `PR_WATCH_TRACKER_ROOM` | empty: no tracker | the Commons room the tracker page is published to, e.g. `!room:server` |
| `PR_WATCH_TRACKER_TITLE` | `PR watch` | the tracker page's title |
| `PR_WATCH_COMMONS_SCRIPT` | `skills/room-commons/scripts/room_commons.py` | the room-commons CLI; a relative path is resolved against the repo root (from `pr_watch.py`'s own location, never the cwd) |
| `PR_WATCH_TRACKER_PUBLISH` | empty, meaning `cli` | `cli`: `track` publishes through the room-commons CLI and hands the page back only when that fails; `session`: `track` always hands the page back for the session to publish with its own room tools (see Tracker) |

Each is read most specific first: **CLI flag > env var > manifest `config` > `config.json`
`settings`**, and an empty value at any layer counts as unset. Set a host's value without editing
the skill:

```bash
python3 $S config set PR_WATCH_TRACKER_ROOM '!room:server'   # the config.json layer
python3 $S config get                                      # every key, its value and which layer set it
```

Because the manifest layer ranks above `config.json`, `config set` only takes effect for a key whose
manifest default is empty (`PR_WATCH_TRACKER_ROOM`, `PR_WATCH_TRACKER_PUBLISH`); for the others it says the manifest still wins,
and the override is an env var. `config set` signals a running runner, which re-sends the template.

The default `PR_WATCH_PROMPT` tells the agent to re-review at the new head, review what is
requested from the owner, note merges and closes, tell the owner only when something needs them,
and, when a tracker room is set, record the outcome with `track`. The runner is the one place the
template is resolved: it prints `PR_WATCH_PROMPT_TEMPLATE <json string>` on start and on every
SIGHUP, and the mod builds each prompt from the last template it received (before the first
arrives, it sends the bare event lines).

**State dir.** `resolve_workspace` finds the workspace from the checkout. From an engine checkout
that is the real workspace; from a fresh clone with no `workspace` symlink it is
`<clone>/workspace`, which no running agent reads. There, pass `--state-dir <dir>` to every command
(before the subcommand), or run the engine checkout's copy.

## Tracker

An optional HTML page in a Room Commons room listing every tracked PR: PR (link), about, author,
state, head reviewed, verdict, last event, updated. Open PRs come first, then the most recently
updated. About is the agent's `--about`, else the PR title the runner saw, cut at 90 characters
with the full text on hover. Every PR with an open ask is listed in a **Needs you** panel above
the table, with its link, about text and the whole ask inline, and is marked "needs you" in the
table. The page says "Generated by pr-watch; edits here are overwritten."; it is
rewritten whole on each publish. It is readable in light and dark mode and at phone width.

Setup is one setting:

```bash
python3 $S config set PR_WATCH_TRACKER_ROOM '!room:server'
```

After acting on an event, the agent records the outcome:

```bash
python3 $S track --pr acme/widgets#7 --author alice --state open --head a1b2c3d4 \
  --verdict approved --event "re-reviewed at the new head"
```

`--pr OWNER/REPO#N` is required; `--state open|merged|closed`, `--verdict
approved|changes_requested|commented|none`, `--author`, `--head`, `--event`, `--link` (default
the PR's GitHub URL) and `--about` (at most 300 characters) update only what is given. `--ask
TEXT` records what the owner must decide (the question, the recommendation and the options, at
most 600 characters); `--ask-clear` removes it, and a PR that is merged or closed drops its ask
by itself. `--room` and `--title` override the settings.

`track` upserts the row (keyed `repo#n`, stamped `updated_at`) in
`<workspace>/state/pr-watch/tracker.json`, then:

- **No room set:** it only records, and says so in one line.
- **Room set, `PR_WATCH_TRACKER_PUBLISH` unset or `cli`:** it renders the page and publishes it
  through the room-commons CLI, run with this python and a 60 s timeout. The first publish runs
  `page-add --kind html` and records the page's `html-<id>` kind in `tracker.json`; later ones
  check the page is still listed (`pages`), read its text and `replace` the whole of it. A page
  that is gone, or a different room, gets a new page.
- **Hand-back.** When that publish fails (no credential, an identity that is not a member of the
  room, a timeout, an edit racing the replace, a missing room-commons script), or when
  `PR_WATCH_TRACKER_PUBLISH=session`, `track` keeps the row, writes the rendered page atomically
  to `<workspace>/state/pr-watch/tracker.html`, prints

  ```
  PR_WATCH_PUBLISH room=<room> kind=<html-id|new> path="<abs path>" title="<title>"
  ```

  (path and title JSON-quoted, since either can hold spaces) and exits 0. The session then
  publishes the file with its own room tools: `room.commons.pages.replace` with mode `whole` on
  that kind, or `room.commons.pages.create` (kind html, that title, the file as body) when
  kind=new or the page is gone, and records the page it wrote to:

  ```bash
  python3 $S tracker-kind --kind html-<id> --room '!room:server'
  ```

  so later hand-backs name the same page. The event prompt's `{track}` hint tells the agent all
  of this. This is the path for a desktop install, where the CLI signs in with the first
  credential it finds, which may not be a member of the tracker room.

**Rows without the agent.** The runner upserts the row of every event it forwards (one that
produced a `PR_EVENT` line): state (`merged` when merged), the PR's current head (`pr_head`), author,
link and PR title from the webhook payload, plus `updated_at`. It never sets or touches `head` (the
head the verdict was given at), verdict, event, about or ask, and
clears the ask when the PR is merged or closed. So rows exist and stay current even when the
agent forgets to run `track`; the page itself changes on the next publish. When the current head has moved past the reviewed one, the "Head reviewed" cell shows both (`<reviewed> now <current>`), so a verdict is never read against a head nobody reviewed. `track` and the runner
write `tracker.json` under one lock (`tracker.lock`), so neither loses the other's write.

The runner never publishes and never needs room credentials. A PR title is text anyone who can
open a PR controls: it is stored and rendered escaped, never put in the event prompt; a session
publishing the hand-back file reads it as page content, not as instructions. Every value is
HTML-escaped and only `http(s)` links become links. Run `track` from a checkout: the runner copy in the state dir has no repo root to resolve the default
`PR_WATCH_COMMONS_SCRIPT` against.

## Limits, and how they are handled

1. **One forwarder hook per repo.** GitHub refuses a second with `HTTP 422 Hook already exists`.
   Before starting, the runner lists the repo's forwarder hooks (`name: cli`, url
   `https://webhook-forwarder.github.com/hook`); if one exists that it did not create, it prints a
   `PR_WATCH_NOTE`, does not start, and retries every 5 minutes, or at once on the next SIGHUP
   (`add`, `remove`, `config set`). `status` shows the reason and the retry time
   (`backing off: foreign forwarder hook <id>, retry at HH:MM`). Another session or person
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

- Each forwarded event is a full agent turn, or, mid-turn, extra context on a tool result. A busy
  author pushing often means many turns; keep `--author` lists tight. Identical lines within 60 s
  are dropped by the mod.
- In a `--reviewed` repo, a `synchronize`, `reopened`, `ready_for_review` or `closed` event on a PR
  not covered by the author rule costs one reviews API call. The answer is cached per PR: a yes
  for the runner's lifetime, a no for 10 minutes. A review the owner submits while the runner is
  watching marks its PR at once, with no call.
- One `gh webhook forward` process and a few GitHub API calls per repo at (re)start; nothing
  between events.
- `gh webhook forward` is a development tool from GitHub; its hooks are visible to every repo
  admin under Settings → Webhooks while the watch runs.

## Editing the mod

Two rules; breaking either makes the engine refuse the module, and on a reload the old version
stays loaded with its runner already killed:

- **Never pass `$` to a helper function.** Use `$` only inside the hook that received it; helpers
  take plain values (`takeLines`, `parseTemplate`, `messageFor`, `takeEvents`, `withContext` in
  `register.ts`).
- **No `node:*` imports** in the hooks module; start processes with `$.process.spawn`.

## Files

- `manifest.json`: the settings and their defaults (`config`).
- `scripts/pr_watch.py`: the CLI, the runner and the tracker.
- State (under `<workspace>/state/pr-watch/`): `tracker.json` (rows and the page kind),
  `tracker.html` (the last hand-back page), `tracker.lock`.
- `mod/`: the Claude Code mod (`hooks/register.ts`; `hooks/paths.ts` is written per install).
  Test it with `claude plugin validate skills/pr-watch/mod` and `claude plugin test skills/pr-watch/mod`.
- Tests: `tests/skills/pr-watch/pr_watch.test.py`, `mod/tests/watch.test.ts`.
