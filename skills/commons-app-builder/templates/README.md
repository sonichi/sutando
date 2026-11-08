# Starter templates

Four complete apps to start from, one directory each: `page.html` (the interface) and
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

### What they do not enforce

A page is a convenience over a shared database, not a permission boundary. Anyone the room lets
write can still open the table view and edit any row — change a vote's `Choice`, add a move, mark
someone else's item done. What the templates *do* guarantee is that **authorship cannot be
forged** (`created_by` is stamped by the server) and that the pages never present a forgeable
field as identity. Say this plainly when you hand an app over, rather than implying the page
controls who may do what.

A sign-up sheet is the worked example in [GUIDE.md](../GUIDE.md), not a template here.
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
