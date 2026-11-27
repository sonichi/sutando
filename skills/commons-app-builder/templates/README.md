# Starter templates

Six complete apps to start from, one directory each: `page.html` (the interface) and
`schema.json` (the databases, their columns and views). They are starting points, not a
library: copy one, rename the database and columns to the real subject, delete what the
request does not need, and keep following [GUIDE.md](../GUIDE.md) — the data model, the
verification in step 6 and the reply in step 7 are still yours to do.

Each page follows the whole UX bar already: phone width first with no horizontal scroll,
44px targets, light and dark, a loading, empty, error and waiting state, writes that retry
while the viewer has not answered the consent prompt, and ownership shown as `?` rather than
`0` until `personId` can answer.

## Which one

| Template | Use it when | Shape |
| --- | --- | --- |
| [`tracker/`](tracker/) | Work with an owner and a deadline: issues, chores, requests, a review queue. The one to pick when an **agent should do the work** on a row. | One database. `Status` drives a board; `Actor` + the database's summon policy hand a row to an agent, which writes back into `Outcome`. |
| [`poll/`](poll/) | A decision to take: a vote, a preference, a show of hands, picking a date. | One row per vote, **one vote counted per person**: the tally groups by `created_by` and takes each person's latest row, so repeat rows cannot inflate a count. Results are live. |
| [`planner/`](planner/) | Anything dated across a week: a rota, a schedule, a meal plan, a trip. | Dated rows plus a day part. Seven columns on a wide screen, one scrolling list of days on a phone. The week on screen is per viewer, in the hash. |
| [`turn-game/`](turn-game/) | Two people taking turns: tic-tac-toe as shipped, and the shape for gomoku, checkers or any board game. | Append-only move rows, replayed in insertion order. Both the turn **and each mark** follow the move count, so a row claiming the wrong seat is skipped rather than drawn out of turn. |
| [`research-desk/`](research-desk/) | Members and their own agents gather material on a question: a reading list, a literature review, due diligence, fact-checking. A finding counts only once a **person accepts it**. | Two databases. Findings carry a source, a basis and what they change, and stay **proposed** until a person accepts or rejects them in Reviews, whose server-stamped `created_by` is the accepter. A report view assembles accepted findings by topic. Each member can **ask their own agent** about a row or a topic. |
| [`task-board/`](task-board/) | People hand work to their **own agents** and the room follows it: errands, ops requests, a team's to-do list with agents doing items. | One database. A task names its agent in `Actor` when the agent is in the room, so the row summons it and the agent reports `Status`, `Blocker` and `Result` on the row. An agent outside the room is asked in its owner's DM instead. Board columns by status. |

### Asking an agent: what comes back, and what does not

`research-desk/` and `task-board/` ask agents with today's page interface only
([GUIDE.md](../GUIDE.md), "Asking an agent from an app"):

- **`artifact.agent.sendTask`** (both): the viewer's own agent, after they confirm the exact text;
  the task goes to that agent's private DM with the viewer. The page gets a "sent" receipt and
  nothing else: **no progress, no completion, no result**. Both pages say so where the button is.
- **Row Summon by `Actor`** (`task-board/`): the agent works under its own room authority and
  writes `Status`, `Blocker` and `Result` on the row, which is the only progress the room sees.
  Creating a row with `Actor` already set summons the agent, as a later change does, under the
  database's summon policy (`owner` here: only the agent's owner's writes summon it). What can
  fail is delivery: a row summon reaches an agent only through its own runtime's inbox, or an MCP
  agent subscribed to `agent.trigger` events covering row summons (`commons.summon`); an agent
  that listens only for mentions never gets it. So when a row is still Requested a minute after
  it was added, the board says so and offers "Tell <agent> in the room": a mention the viewer
  confirms, which reaches the agent either way. That line is per tab: only the viewer who added
  the row sees it, in the tab they added it from, until they reload (a page's rows carry no
  created time to work it out from).
- What an agent finds reaches `research-desk/` only if the agent writes new findings rows itself.
- The access question ("Allow this page…") usually comes as the page opens, because both pages read
  members and agents at load. While it is open, the agent picker says it is waiting; after a
  Cancel it says the page was not allowed and to reload, since no second question comes in that
  tab. A write that lands later also makes the page read the agents again.
- Planned, not available yet: the room will see the *state* of a `sendTask` request (working, needs
  input, done or failed) and whose agent holds it, never the request text, the DM answer or its
  outcome line. The requester is told so in the confirmation dialog. A result is still only what
  the agent itself writes into the app's room. `task-board/` already words its notes this way.

### What they do not enforce

A page is a convenience over a shared database, not a permission boundary. Anyone the room lets
write can still open the table view and edit any row — change a vote's `Choice`, add a move, mark
someone else's item done. What the templates *do* guarantee is that **authorship cannot be
forged** (`created_by` is stamped by the server) and that the pages never present a forgeable
field as identity. Say this plainly when you hand an app over, rather than implying the page
controls who may do what.

A sign-up sheet starts from `poll/`: one choice per person among fixed slots is poll's model
(each person's latest row is the one counted, so changing slot replaces the sign-up). Rename the
`Choice` options to the slots; a page that lists who signed up for each slot, as the worked
example in [GUIDE.md](../GUIDE.md) does, is the agent's own `html` with its own `schema`.
For something with no obvious shape, read the "Patterns from apps that shipped" section.

## Using one

1. **Create the databases** from `schema.json`: `pages.create {kind: "db", title: <name>,
   template: "blank"}`, then `databases.mutate` op `prop_add` for each entry in `props`, and
   op `view_add` for each entry in `views`. The blank template mints the title column, so the
   schemas never declare one — read its name from `databases.list`.
2. **Create the page** with `pages.create {kind: "html", title, body: <page.html>}`, then
   `apps.create {name, database: "db:<id>", page: "html-<id>"}`.
3. **Rename** the placeholder options. `poll/`'s choices are `Option A/B/C` on purpose: change
   them with `prop_update` before anyone votes.
4. **Re-verify after editing.** Run the pre-flight, then GUIDE step 6:

   ```bash
   python3 skills/commons-app-builder/scripts/check_page.py page.html
   ```

## From the panel

The s.space panel can ask for one of these by name in a build request (`template`:
`tracker`, `poll`, `planner` or `turn-game`); see [BUILD_REQUEST.md](../BUILD_REQUEST.md).
`research-desk` and `task-board` are not offered there yet: an agent copies them by hand.
`scripts/build_request.py` renames the app, its database and its options and writes the page in
chunks of at most 20,000 bytes.

## What the tests cover

- `tests/commons-app-builder-templates.test.mjs` runs every page against a fake
  `window.artifact` and checks each control's data path: the exact `createRow` and `write`
  calls, the empty and error states, consent pending, a refused write retrying, and the
  game's replay, turn order and win detection.
- `tests/commons-app-builder-templates.test.py` checks each schema (types, options, groups,
  `created_by` present, no declared title column, views pointing at real columns), that every
  column a page names exists in its schema, and the phone-first CSS contract.

Neither can click a real consent prompt, show a second viewer syncing live, or lay out CSS:
only a browser and a real room do those. Say so when you report, as GUIDE step 6.4 asks.
