# commons-sdk

An optional helper for people and agents who write Room Commons apps. One file, no dependencies.
Paste it into your HTML page and you get shared maps, shared lists, players, claims and a way to
ask an agent.

Version 0.1.0. First version: built and tested against a fake room only. Nothing here has run in
a real room or a browser. See [`NOTES.md`](NOTES.md) for what a real room must still confirm.

## Where it fits

- **It is optional.** Existing apps and the starter templates do not load it, by design: nothing
  changes for them. [`GUIDE.md`](../GUIDE.md) is still the whole method for building an app. Use
  the library when a page would otherwise write its own snapshot handling, write retry and
  rollback. A page that needs none of that is simpler without it.
- **It must be inlined.** A page runs in a sandbox with no network, so it cannot load a script
  from anywhere. Copy `commons-sdk.js` into a `<script>` in the page. `examples/inline.js` shows
  one way to do that between two marker comments. The file is about 33 KB; a page body is at most
  196,608 bytes. Once it has run, the global `Commons` exists, whatever other scripts the page
  carries (it also sets `module.exports` when a `module` with `exports` is there, as in Node).
- **The bridge is not ours.** The library is built on the page API (`window.artifact`). The host
  that runs the page owns that contract. In this repo the reference is the guide, section
  ["What you work with"](../GUIDE.md#what-you-work-with). When this README and the guide
  disagree, the guide is right and this file has a bug.
- **It is not a CRDT.** Two writes to one place do not merge. See "Semantics and limits".

### Which templates it suits

| Template | Fit |
| --- | --- |
| Sign-up sheet (the guide's worked example; `templates/poll/` is the closest starter) | Good. `examples/signup.html` is this app: a list, one row per sign-up, cancel by a marker |
| Task board (`templates/task-board/`) | Good. `examples/board.html` is this shape: a list, a status column, claim and release |
| Tracker (`templates/tracker/`) | Likely: it is the same list shape. Not tried |
| Turn game, poll tally, planner, research desk | Not the target. They replay or group rows in their own way; the library adds little |

The shipped templates do not use the library today. The two examples here are separate pages.

## Quick start: a shared counter

Ask an agent to create a database named `Counter — State` with a text column `Value`. Then:

```html
<button id="up">+1</button> <output id="n"></output> <p id="msg"></p>
<script>/* paste commons-sdk.js here */</script>
<script>
(async () => {
  const room = await Commons.connect();
  const state = await room.map('Counter — State');
  const draw = () => { document.getElementById('n').textContent = state.get('count') ?? 0; };
  state.observe(draw); draw();
  const msg = document.getElementById('msg');
  document.getElementById('up').onclick = async () => {
    try { await state.update('count', (n) => (n ?? 0) + 1); msg.textContent = ''; } catch (e) {
      msg.textContent = e.code === 'OUTCOME_UNKNOWN'
        ? 'Not sure that press was saved. Check the number, and press again if it is missing.'
        : 'Not saved: ' + e.message;
    }
  };
})();
</script>
```

Everyone with the page open sees the number change. An agent can change it too, by editing the
row through Actions.

`update` reads the value, runs your function on it and writes the result back. It resolves to
`{value, mode}`. Where the room offers conditional writes, `mode` is `"exact"` and no press is
lost. Where it does not, `mode` is `"best-effort"`: two people who press at the same moment both
write the same new number and one press is lost. See "Conditional writes".

One case the page must show, as above: `OUTCOME_UNKNOWN`. The room gave no answer, so the press
may or may not have been saved, and the library does not guess or press again on its own. Tell
the person, let them look at the number and decide. For a count that must not lose a press in
any room, push one row per press into a list and show `list.length`.

## What you need in the room

A page cannot create a database or a column. An agent (or a person) creates them first.

| Type | The database needs |
| --- | --- |
| `room.map(name)` | The title column (used as the key) and a text column `Value` |
| `room.list(name)` | Any columns you like |
| `list.remove(id)` | A text column `Removed`, or your own marker (`removed: {prop, value}`) |
| `list.claim(id)` | A text column `Claim` |

Find a database by its exact name, or leave the name out to use the app's own database.

## API

### Connect

```js
const room = await Commons.connect();
```

Rejects with code `NO_BRIDGE` when the page is opened outside a room.

| Member | What it gives |
| --- | --- |
| `room.me()` | `{id, name, kind}` for the viewer. `kind` is `"person"` or `"agent"` |
| `room.players()` | Promise of the room's joined members, same shape, plus `avatar` when there is one |
| `room.playerOf(cell)` | Promise of the player id that a "created by" or person cell names, or `null` |
| `room.app` | `{id, name, database}`, or `null` when the page is opened on its own |
| `room.capabilities` | `{players, authorship, agents, post, requests}`, each `true` or `false` |
| `room.now()` | Room time in milliseconds (the bridge's estimate; this device's clock until it has one) |
| `room.onWaiting(cb)` | `cb(code)` each time a write waits and retries. Returns a stop function |
| `room.map(name?, opts?)` | Promise of a shared map. `opts.valueProp` (default `"Value"`) |
| `room.list(name?, opts?)` | Promise of a shared list. `opts.orderBy`, `opts.removed`, `opts.claimProp` |
| `room.agents()` | Promise of the viewer's own agents, `[{id, name, kind: "agent", inRoom}]` |
| `room.ask(agentId, text, {key, actionId?, row?})` | Asks one of the viewer's own agents. Promise of `{eventId}` |
| `room.post(text, {key?})` | Posts in the room as the viewer. Promise of `{eventId}` |
| `room.mention(playerId)` | The text that mentions a player in `post` |
| `room.close()` | Stops the room's watches |
| `room.experimental.requests` | See "Experimental". `null` today |

### Shared map

```js
const m = await room.map('My app — State');
```

| Member | What it does |
| --- | --- |
| `m.get(key)` | The value, or `undefined` |
| `m.has(key)` | `true` when the key holds a value |
| `m.entries()` | `[[key, value], …]` |
| `m.size` | How many keys hold a value |
| `m.set(key, value)` | Promise. Any JSON value. Shown at once; undone if the room refuses |
| `m.update(key, fn)` | Promise of `{value, mode}`. Read, change with `fn(current)`, write back. `mode` is `"exact"` or `"best-effort"`. See "Conditional writes" |
| `m.delete(key)` | Promise. Empties the value. The row stays |
| `m.observe(cb)` | `cb(changes, origin)`. Returns a stop function |
| `m.databaseId`, `m.gone` | The database id; `true` once the database was removed |

A change is `{key, action: "add" | "update" | "delete", value?, oldValue?}`.

### Shared list

```js
const tasks = await room.list('My app — Tasks', { orderBy: 'Order' });
```

| Member | What it does |
| --- | --- |
| `tasks.toArray()` | The rows, `[{id, cells}]`. `cells` is keyed by column name |
| `tasks.get(id)` | One row, or `undefined` |
| `tasks.length` | How many rows |
| `tasks.titleProp` | The name of the title column. Read it; never assume it |
| `tasks.push(cells)` | Promise of the new row's id. All values in one call: all fit, or nothing is created |
| `tasks.update(id, cells)` | Promise. Shown at once; undone if the room refuses |
| `tasks.remove(id)` | Promise. Hides the row by writing the "removed" marker |
| `tasks.claim(id, {ttl?, onLost?})` | Promise of `{ok, holder, certainty, reason?}`. See "Claims" |
| `tasks.release(id)` | Promise of `true`, or `false` when you did not hold it |
| `tasks.claimOf(id)` | `{id, name, kind, until, mine}`, or `null` when free |
| `tasks.observe(cb)` | `cb(changes, origin)`. Returns a stop function |
| `tasks.databaseId`, `tasks.gone` | As on the map |

A change is `{id, action: "add" | "update" | "delete", row?, oldRow?}`.

`origin` is `"local"` for this page's own call (and for its undo) and `"remote"` for a change
that came from the room. `observe` fires only when something really changed.

Rows are in the order the bridge returns them, or sorted by `orderBy` (blank values last, row id
as the tie-break). A row you just pushed has no server-filled cells (such as "Created by") until
the next change arrives from the room.

### Errors

Every failure is a `Commons.CommonsError` with a stable `code`. `reason` holds the bridge's own
words when there are any.

| Code | Meaning |
| --- | --- |
| `NO_BRIDGE` | The page is not open in a room |
| `DB_MISSING` | No database with that name, or no name and the page is not an app |
| `DB_GONE` | The database was removed while the page was open |
| `SCHEMA` | A column the call needs is missing |
| `NOT_FOUND` | No row with that id |
| `BAD_ARGUMENT` | A key, ttl or value the library cannot send |
| `TOO_LARGE` | The write is over 20,000 bytes. Checked before sending |
| `CONSENT_PENDING` | The viewer has not answered "may this page edit data?" (after 2 minutes of retries) |
| `CONSENT_DENIED` | The viewer said no |
| `CONSENT_REQUIRED` | Members, authors and agents need the viewer's yes first |
| `RATE_LIMITED` | Too many writes, reads, asks or posts in a minute |
| `DECLINED` | The viewer cancelled an ask or a post |
| `AGENT_UNAVAILABLE` | The agent is not the viewer's, or cannot be reached |
| `BUSY` | Another ask is open |
| `OUTCOME_UNKNOWN` | No answer came back. For an ask: send again with the same key. For `map.update`: the write may or may not have landed; `value` (and `version`, when there is one) hold what this page sees now. Show it and let the person decide; do not repeat it blindly |
| `OFFLINE`, `APPROVAL_PENDING` | A conditional write was not sent because the page is offline, or is held for an approval and has not landed yet |
| `BAD_MENTION` | A mention names someone who is not a joined member, or a group |
| `UNSUPPORTED` | This bridge does not have the call |
| `CONTENDED` | `map.update` gave up: the database kept changing between its read and its write |
| `REFUSED` | Any other refusal. Read `reason` |

Writes retry on their own while the viewer has not answered or the page is rate-limited: every
1.5 seconds, for up to 2 minutes. Use `room.onWaiting` to show a hint such as "Allow this page to
edit data, and it will save". Limits are in `Commons.LIMITS`.

## Coming from Yjs

| Yjs | Here | Notes |
| --- | --- | --- |
| `Y.Map` | `room.map(name)` | `get`, `set`, `delete`, `has`, `entries`, `size`, `observe` |
| `Y.Array` | `room.list(name)` | `push`, `toArray`, `get`, `length`, `observe`; `update` and `remove` by row id, not by index |
| awareness | `room.players()`, `room.me()` | The room's members. Not "who is online" |
| provider | the room | `Commons.connect()`. The host holds the connection |
| `observe(event)` | `observe(changes, origin)` | A plain array of changes. `origin` is `"local"` or `"remote"` |
| `doc.transact` | none | Each cell is its own write |

What does **not** carry over:

- **No CRDT merge.** Two writes to one place do not merge. The last one wins.
- **No offline.** A write needs the room. There is no local store and no replay.
- **No text type.** No `Y.Text`, no shared cursor, no editor binding.
- **No index positions.** A list row has an id, not a position. There is no `insert(index)`.
- **No transactions and no undo manager.**
- **Writes are promises.** A write can be refused. Await it and show the reason.
- **No nested types.** A map value is plain JSON, replaced as a whole.

## Semantics and limits

This is not a CRDT. The rules below are what the library does on top of the bridge.

- **Map: last write wins, per key.** Two pages set one key at the same moment: the write that
  reaches the room last is the value everyone ends up with. The other write is lost without an
  error.
- **Map: a new key created twice.** Two pages set a key that does not exist yet: the room gets
  two rows. Every page then uses the row with the lowest row id, so all pages agree. The other
  row stays in the table and is ignored. This is most likely on an empty map, when two pages
  write their first value at the same moment. `map.update` in `"exact"` mode creates the row
  with a conditional write, so only one row results; `set` and `"best-effort"` do not.
- **Map: `delete` with two rows for one key.** `delete` empties only the row in use, the one
  with the lowest id. If that row is later removed through Actions, the other row's old value
  comes back.
- **List: last write wins, per cell.** A cell is one column of one row. Two pages change
  different columns of one row: both changes stay. Two pages change the same column: the last
  one to arrive wins.
- **List: `update` with several columns is not atomic.** Each column is its own write. If one is
  refused, the earlier ones stay saved; the error's `written` lists them.
- **Local changes show at once.** A map's `set` and `delete`, and a list's `update`, `remove`,
  `claim` and `release`, show on this page before the room answers. If the room refuses, the
  change is undone and `observe` fires again. Two exceptions: a list's `push` and a map's
  `update` show when the bridge answers.
- **Writes from one page run in order,** one at a time per database.
- **Nothing is ever deleted.** A page cannot delete a row. `map.delete` empties the value;
  `list.remove` writes a marker and hides the row.
- **Everything is readable and editable by the room.** A page is a convenience, not a
  permission boundary. Anyone the room lets write can open the table and change any cell.
- **Limits** (the bridge's, as the guide gives them): 30 writes and 20 reads a minute per page, 20,000 bytes per
  write, one watch per database, at most 200 members listed. `ask`: 10 a minute per person and
  agent. `post`: 10 a minute, at most 20 mentions.

### Conditional writes

A host can give each database snapshot a `version` and accept "write only if the database is
still at this version". Both sit behind host switches and can be off. The library looks for
them at run time:

- A snapshot (from the first read, or from a change) with a number in `version`: the library
  keeps it. A snapshot without one means "not available here".
- With a version, `claim`, `release` and `map.update` send their write with `{ifVersion}`. If
  the host answers that the version changed, the library reads again and decides again, at most
  five times. If it answers that conditional writes are not available, or that the request is
  invalid, the library writes plainly from then on.
- A successful answer carries the database's new version when the server returns it. The
  library builds its next write on that version and on the value it just wrote, so several
  updates in a row work even while the page's own copy is behind. A snapshot older than that
  version is ignored; a newer one wins. If the answer has no version, the next write waits for
  the page's copy to catch up (and ends as `CONTENDED` if it does not).
- No answer to a conditional write means the outcome is unknown: it may have landed. The host
  offers no key to resend the same write safely and no receipt to look it up, so:
  - `map.update` rejects with `OUTCOME_UNKNOWN`. It does not treat an equal value as proof (a
    peer can write the same number) and does not run your function a second time.
  - `claim` and `release` read again and go on, because a claim carries its own proof: each
    attempt writes a fresh token, and "the cell holds a token this call wrote" cannot be
    someone else's doing.
- `"exact"` therefore means: the room accepted this write at the version this page had read,
  and said so.

Three things differ from a plain write, by the host's design:

- A conditional write is never queued. It reaches the room now or it fails now.
- It never enters the page's own copy ahead of the room. It is not shown early by the host.
- It is not recorded as the page's write for automations.

A plain write is different: it goes into the page's own copy first, and the page's reads answer
from that copy. So after a plain write, reading back shows the page its own value and proves
nothing about the room. The library therefore does not read back after a plain write.

Where this comes from. The call shape and the refusal reasons are documented in
`docs/commons-html-page-api.md`
in the web client (cinny-webclient) repository, section
"Database versions and conditional writes". That document owns the contract, so it is
not repeated here. What is still unchecked: the library has run only against the fake bridge's
model of that section, never against a real room. `NOTES.md` lists what a live check must
confirm. `set`, `delete`, `push`, `update` (list) and `remove` always write plainly.

### What is attested, and what is not

- `room.me()` and `room.players()` come from the host. A page cannot change them.
- `room.playerOf(row.cells['Created by'])` is the real author of a row: the room stamps that
  cell. Use it for "is this row mine?". Whether stamping is switched on is a deployment setting.
- Anything a page writes into a cell is **not** attested. That includes a name typed into a
  form and the holder inside a claim. Do not use either as proof of who someone is.

## Claims

`list.claim(id, {ttl})` marks a row as held by the viewer for `ttl` milliseconds (default one
minute). The claim is JSON in the row's `Claim` cell:

```json
{"by": "<player id>", "name": "Ana", "kind": "person", "until": 1790000000000, "n": "<token>"}
```

An agent without a page claims the same way: it writes that cell through Actions (`n` is
optional), reads the row back, and treats the claim as its own only if its value is still there.

**What `claim` resolves to.** `{ok, holder, certainty, reason?}`.

| Result | Meaning |
| --- | --- |
| `ok: true, certainty: "room"` | The room accepted the claim with a conditional write. Nobody else's claim was there when it landed |
| `ok: true, certainty: "local"` | A plain write. Only this page's own copy shows the claim. Another page may have claimed too; if so, `onLost` is called once the copies meet |
| `ok: false, reason: "held"` | Someone else holds the row. `holder` says who. Nothing was written, or the room refused the write |
| `ok: false, reason: "expired"` | The write was accepted, but so late that the claim had already run out |
| `ok: false, reason: "contended"` | The database kept changing, or the claim did not stick. Try again |

`certainty` is `"local"` on every `ok: false`: the answer rests on what this page can see.

**The race rule.**

1. If this page already sees a live claim by someone else, `claim` writes nothing and resolves to
   `{ok: false, reason: "held", holder}`.
2. **With conditional writes,** the room takes one of two simultaneous claims. The other page is
   told the version changed, reads again, sees the holder and gets `reason: "held"`. Two pages
   cannot both get `certainty: "room"` for one row.
3. **Without them,** each page writes into its own copy and both get `{ok: true, certainty:
   "local"}`. When the copies meet, the claim that reached the room last stays, and the other
   page's `onLost(holder)` is called. The window in which both believe they hold the row is the
   sync delay between the two pages: usually short, but as long as a page stays out of touch.
4. So treat `"local"` as "probably mine". Show it as unsettled, keep `onLost` wired, and do slow
   or costly work only when it is safe to do twice. `examples/board.html` shows one way.
5. The expiry is stamped when the write is sent. A write that waits (for the viewer's answer, or
   a rate limit) is stamped again on each try, so a claim never comes back `ok` with its time
   already used up.
6. A claim ends at `until`, by `room.now()`. Nobody has to clear it. `claimOf` then returns
   `null`, and anyone may claim. No event fires when a claim runs out: redraw on a timer if you
   show it.
7. Claiming a row you hold renews it. `release` empties the cell; with conditional writes it
   does not wipe a claim someone else took in the meantime. If a renewal or a release is refused,
   the earlier claim is still tracked, and `onLost` still fires if it is taken over.

Claims are cooperative. Any member can overwrite the cell by hand. They stop honest players
from doing the same work twice; they do not lock anything.

## Asking an agent

- `room.ask(agentId, text, {key})` asks one of **the viewer's own** agents. The viewer sees the
  exact text and must confirm. The task goes to the agent's private chat with the viewer.
- `room.post(text)` posts in the room as the viewer, after they confirm. Add
  `room.mention(id)` to call an agent or a person who is in the room.
- A row change can summon an agent. That is set up on the database, not in the page.

The page gets a receipt (`eventId`) and nothing else: no progress, no result, no reply. This
library does not read replies. When the page must show an outcome, have the agent write it into a
row (for example `Status` and `Result` columns) and read it with `list.observe`.

## Experimental

`room.experimental.requests` is an adapter for the named-agent request calls
(`agent.requestable`, `agent.request`, `agent.requests`). **Those calls are designed and not
built. Their shape may change, and so may this adapter.** It is `null` unless the bridge has all
three, so check it before use:

```js
const rq = room.experimental.requests;
if (rq) {
  const agents = await rq.agents();                      // [{id, name, actions}]
  await rq.watch({ database: tasks.databaseId }, render); // render(wholeList), now and on change
  const { requestId, duplicate } = await rq.send({ agentId, action: 'claim', row: { database: tasks.databaseId, rowId }, text: 'Claim "…"', key: 'claim-' + rowId });
}
```

A refused `send` rejects with `DECLINED`, `CONSENT_REQUIRED`, `RATE_LIMITED`, or
`REQUEST_REFUSED` with the platform's code in `reason`. Nothing else in the library depends on
this adapter.

The request list reaches `render` exactly as the bridge gave it, each request's `state` and
`outcome` included. The library never fills or changes `outcome`. It is text someone else wrote,
so put it on the page as plain text and never as HTML:

```js
function render(list) {
  box.replaceChildren(...list.map((r) => {
    const line = document.createElement('p');
    line.textContent = r.outcome ? r.state + ': ' + r.outcome : r.state;
    return line;
  }));
}
```

The same rule holds for every value that comes through the bridge: names, cell values, a claim's
holder, a refusal's `reason`. Use `textContent`. Both examples do.

## Not built yet: needs the platform

Each line is something the documented bridge does not let a library do.

1. **Delete a row from a page.** There is no page call for it, so `remove` and `delete` leave rows behind. The list's marker lives in one place (`removal`), so a real delete can replace it there.
2. **Write several cells of one row in one call.** `update` and "claim and set status" are not atomic.
3. **Write only if unchanged.** This exists, per database, behind host switches (see "Conditional writes"); the library uses it when present. Still missing: the same check per row or per cell. Where it is off, claims have a race window and counters lose presses.
4. **A unique title per database.** Without it two pages can create one map key twice.
5. **A server-stamped time on a row or a cell.** Claim expiry rests on a time the claimer wrote.
6. **An attested "last edited by" a page can read per cell.** Without it a claim's holder is self-declared.
7. **Presence a page can read:** who has the app open now, people and agents. `players()` is membership only.
8. **A documented row order** for `db.read` (creation order or other). The default list order assumes it is stable.
9. **Stable refusal codes** from `db.write`, `db.createRow` and `db.read`. Today the library matches reason text, in one function (`refusal`), so codes can replace it there. Reasons the guide does not quote, such as a write waiting for an approval, a page that is offline, or no answer from the room, come back as `REFUSED` with the text in `reason`.
10. **Create a database or a column from a page** (or an install step). Today an agent must create the schema first.
11. **A documented path for an agent's reply or outcome to reach the page.** None exists, so the library offers no reply reading.
12. **Ask a named agent that is not the viewer's own, with state and outcome back.** Designed, not built; see "Experimental".
13. **Documented return and callback shapes for `artifact.state.*` and `artifact.emit/on`.** They are named but not specified, so the library leaves out page state and live events.
14. **A way to see the remaining write and read budget.** Each "version changed" answer costs one of 20 reads a minute.
15. **A shared text or document type for apps,** and a way to pass Yjs updates through the bridge. Needed for real Yjs apps.
16. **Offline writes.** There is no local store in the sandbox and no queue on the bridge.
17. **An idempotency key or a write receipt for conditional writes.** After "no answer from the room" a page cannot resend the same write safely or ask whether it landed, so `map.update` has to report `OUTCOME_UNKNOWN`.

## Files

| File | What it is |
| --- | --- |
| `commons-sdk.js` | The library. Inline it in a page, or `require` it in Node |
| `test/fake-room.js` | A fake bridge: several pages share one store, with consent, limits and a clock you move |
| `test/commons-sdk.test.js` | The library's tests |
| `examples/signup.html` | A sign-up sheet: a form and a live list |
| `examples/board.html` | A task board: a person or an agent takes a task, works, marks it done |
| `examples/_head.css` | The styles both examples share |
| `examples/check.js` | Runs both pages in Node against the fake bridge |
| `examples/inline.js` | Copies the library and shared styles into the examples |
| `package.json` | Marks this folder as CommonJS for Node. Nothing to install |

The examples need these databases, created by an agent or a person first:

- `signup.html`: "Sign-up — Entries" with the title column (the name shown), `Note` (text),
  `State` (select: `Going`, `Cancelled`) and `Created by` (created_by). An agent signs up the same
  way, by adding a row through Actions.
- `board.html`: "Board — Tasks" with the title column (the task), `Status` (select: `Open`,
  `Done`), `Claim` (text) and `Result` (text). An agent without a page plays the same way; see
  "Claims".

From this folder:

```bash
node --test test/*.test.js examples/check.js   # everything
node examples/inline.js                        # after editing commons-sdk.js or _head.css
```

From the repo root, `node tests/commons-app-builder-lib.test.mjs` runs the same tests; CI runs it
with the other `tests/*.test.mjs` suites.
