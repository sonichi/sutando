---
name: room-collab
description: Read and write a room's LIVE collaborative surfaces — the document behind the Doc tab, the whiteboard, the kanban (Yjs/CRDT state, one surface per --kind). Use this when asked to write into, read, watch, or collaborate in any of a room's surfaces. NOT the same thing as `room_ops doc`, which is a room's Context-document folder — a different store entirely.
---

> Formerly `room-doc`. The name changed because the skill serves more than a document — markdown, whiteboard and kanban. The `room-collab` names lead (`ROOM_COLLAB_TOKEN`, `AG2_ROOM_COLLAB_URL`, `/api/v1/room-collab`); the `room-doc` spellings are still read and served for one release, and `skills/room-doc/scripts/room_doc.py` still runs (it forwards here).

# Room Collab

A room's **live collaborative surfaces**: the document a person sees in the Doc
tab and types into, the whiteboard, the kanban. Each is one CRDT state that
several writers edit at once, merged change by change, with each other's
presence visible. `--kind` names the surface; the document is the default.

## Not to be confused with the Context-document folder

| You want | Use |
|---|---|
| The live surfaces people co-edit (document, whiteboard, kanban, deck) | **this skill** |
| A room's stored Context files (`get`/`put`/`rm` by name) | `room_ops context` (formerly `room_ops doc`) |

Two different stores. Writing to one never shows up in the other. This has already
sent one agent to the wrong place, which is why the warning is here and not further down.

## First contact — if you were @-mentioned and have never done this

```bash
P=skills/room-collab/scripts/room_collab.py
python3 $P presence '!room:server'                     # 0. which surfaces are live, who is in them
python3 $P read   '!room:server'                       # 1. the whole document — find the line that names you
python3 $P append '!room:server' $'\n\n@you — <your reply>'   # 2. answer UNDER it, signed
python3 $P read --delta '!room:server'                 # every later return: only what changed since you last read
```

Then say one line in the room ("replied in the doc") — the person who called
you is watching the room, not the document. With the lane env loaded no flag
is needed. If a step fails, `doctor '!room:server'` reports every setup step
(deps, token, URL, connect, read, peers) one line each and names the one that
broke; it is for that, not for reading.

Every `read` remembers what you saw (per room and surface, under the
workspace's `state/room-collab/`), so `read --delta` on your next visit prints
only the lines that appeared since — the way a person skims what is new
before rereading. The first read of a surface is all new. `--json` carries
`delta` and `since` alongside the usual fields.

Use `append` to reply, not `replace`: your text lands where nobody else is
typing, and the merge keeps everyone's characters. `replace` is for editing a
sentence you own.

**To be seen in a surface, register — do not hold it open yourself.** Every
subcommand except `watch` opens the document, does one thing and closes, so
presence published by a `read` is gone before anyone looks. A summon asks you
to *be* there, and your session is the wrong thing to hang that on: it ends,
compacts or restarts, and your presence ends with it.

```bash
python3 $P stay '!room:server'            # after reading a summon
python3 $P --kind board stay '!room:server'
python3 $P stay '!room:server' --leave    # when you are done there
```

`stay` writes a record and exits; it holds nothing and needs no token. The
presence daemon — supervised, outliving any session — reconciles toward that
record, reconnects when a socket dies, and drops a surface after 30 minutes
with no activity on it. Identity and presence name are resolved the same way
every other subcommand resolves them, so the flagless form is correct.

`watch` still exists and still holds a connection, for watching a surface in
the foreground and acting on each event. Use it for that, not for being seen.

**Global flags go BEFORE the subcommand.** `--url`, `--kind`, `--name`,
`--json` belong to the program, not the command: `room_collab.py --kind board
read <room>` works, `room_collab.py read <room> --kind board` is refused as
"unrecognized arguments".

## Requirements

`websockets` and `pycrdt`. Two install routes; which one you need is
decided by the python, not by preference.

**In a virtualenv, or on any python whose pip may install into it:**

```bash
pip install -r skills/room-collab/requirements.txt
```

**On a managed python — Homebrew or a Debian/Ubuntu system python —**
that command refuses with `error: externally-managed-environment`
(PEP 668). Install into a venv and invoke the skill with THAT
interpreter; the skill's own `python3` is not it:

```bash
python3 -m venv ~/.venvs/room-collab
~/.venvs/room-collab/bin/pip install -r skills/room-collab/requirements.txt
~/.venvs/room-collab/bin/python3 skills/room-collab/scripts/room_collab.py read <room>
```

Do not reach for `pip --break-system-packages` to make the first
command work: it writes into the python other services on the host
share.

## Credential

**The agent's ordinary relay token works.** The one every agent already holds
in `channels/<lane>/.env` as `REMOTE_TASK_TOKEN` (or `AG2_REMOTE_TOKEN`) opens
a room's surfaces; the service resolves it to the agent's own Matrix id. No
per-agent Matrix token and no extra grant are needed. A Matrix access token
also works.

The relay token ships in two shapes, **under the same variable names, on
different installs**: bare (`secret`) or compound (`https://host/relay|secret`).
Both are accepted here — the value is inspected, never the name. Passed to
anything else, the compound form must be split on `|`.

Every surface has exactly the **room's own ACL**. core-api distinguishes
non-member (404) from below-write-power (403), while the **WebSocket collapses
every refusal into one close** — from the client you can only see "refused".

Resolution order: `--token`, then `$AG2_MATRIX_TOKEN`, `$ROOM_COLLAB_TOKEN` (`$ROOM_DOC_TOKEN` still read),
`$MATRIX_ACCESS_TOKEN`, `$REMOTE_TASK_TOKEN`, `$AG2_REMOTE_TOKEN`. The service
URL comes from `--url`, then `$AG2_ROOM_COLLAB_URL` (`$AG2_ROOM_DOC_URL` still read), `$AG2_API_ROOT`, the origin
of `$REMOTE_TASK_URL`, or the origin named inside a compound token. With the
lane env loaded, an agent needs neither flag. The socket path defaults to
`/api/v1/room-collab`; a URL that already names `/api/v1/room-doc` is kept as
given while that alias is served.

## Command line

```bash
P=skills/room-collab/scripts/room_collab.py
python3 $P read   '!room:server'                      # print the document
python3 $P read --delta '!room:server'                # only what is new since your last read
python3 $P peers  '!room:server'                      # who is present in THIS surface (opens it)
python3 $P presence '!room:server'                    # who is in EVERY surface, without opening any
python3 $P append '!room:server' 'text to add'        # add at the end
python3 $P replace '!room:server' 'old text' 'new'    # refuses if absent, never writes blindly
python3 $P comment '!room:server' 'the exact words' 'is this final?'   # a comment pinned to them
python3 $P reply  '!room:server' '$eventid' 'yes, final'              # answer in a comment's thread
python3 $P summon '!room:server' '@qingyun:server' --context 'the passage'  # call someone IN
python3 $P --name mars --user-id '@mars:x' watch '!room:server'   # BE PRESENT: held open, so others see you
```

### `summon` — telling someone you need them

Writing `@someone` into the document is just characters: no event, no mention,
no notification. `summon` posts the room message the web client's own @-picker
posts — the same `space.ag2.collab.doc.summon` marker — so their timeline
renders the summon card, with a Join button that opens the surface.

```bash
python3 $P summon '!room:server' '@qingyun:server' --context 'the design doc is ready for you'
python3 $P --kind board summon '!room:server' '@mars:server'      # into the whiteboard
python3 $P summon '!room:server' '@qingyun:server' --dry-run      # see the message, post nothing
```

**One summon is one interruption.** `m.mentions` is what makes the mention real,
which is also what turns it into a task for whoever is called — so this is how
you say "I finished, come and look", not how you decorate a sentence with a name.

`--context` is the passage quoted under the card. You state it; this command does
not check it against the document. It is folded to one line and capped at 400
characters. `--kind` picks the surface (`markdown` default, `board`, `kanban`);
the invitee must be a full mxid, because a bare name renders as prose and calls
nobody.

The card shows the surface, who was called and that passage; it opens the
surface, not the line — the marker carries no anchor.

Add `--insecure` only for a local rig with a self-signed certificate.

## Staying in a surface

A `read` or `append` connects, acts and leaves. To be **in** a surface the
way a person is — told the moment something concerns you, with nobody pinging
you in the room — hold it open:

```bash
python3 $P watch '!room:server'                                         # the text; hears your mxid, its localpart, --name
python3 $P watch '!room:server' --for 'Sutando (qingyun-001)'           # …plus the display name a summon writes for you
python3 $P --kind board  watch '!room:server' --for mars                # the whiteboard
python3 $P --kind kanban watch '!room:server' --for '@you:server'       # the board of cards
#   EVENT<TAB>mention<TAB>where=text<TAB>@mars can you take the second section?
#   EVENT<TAB>mention<TAB>where=board element=t7<TAB>ask @mars about this box
#   EVENT<TAB>assigned<TAB>where=kanban card=c3 column=todo<TAB>write the tests
#   EVENT<TAB>moved<TAB>where=kanban card=c3 from=todo to=doing<TAB>write the tests
#   EVENT<TAB>peer_joined<TAB>who=@qingyun:server name=qingyun
```

One line per event, after `--settle` seconds of quiet (default 1) — the server
forwards one push per keystroke, and a person typing your name is a dozen
pushes. It prints nothing until something concerns you and exits (rc 2, with
the reason) only when the session ends: silence means "nothing yet", never
"not watching". Run it under a monitor and act on each line; reply with
`append` from another invocation, or from the library:

```python
async with open_room_collab(url, room_id, token) as doc:
    async for ev in doc.events(["mars", "@you:server"]):   # every kind, one loop
        if ev["kind"] == "mention": ...                     # act, then doc.append(...)
```

A deploy closes every live connection (close 1012, measured: a held
connection survived 25 minutes untouched and was ended only by a service
restart). `watch` comes back on its own — `RECONNECTING`, then `RECONNECTED` —
carrying its last snapshot, so a line that landed while it was down is still
reported. A refusal (4403 and friends) is an answer about you and is not
retried.

Your own writes are not reported. A bare name in prose ("for mars") is not a
mention; the `@` is what addresses you, and the summon always writes it. Not
yet an event, because it needs the server's authorship record: someone editing
a paragraph *you* wrote.

## The whiteboard is a different surface

A room's board is a second surface — `?kind=board` — and it holds a **map
of drawing elements**, not text. The text commands refuse on it rather than
answering: `read` on a board used to print an empty string, which is
indistinguishable from an empty whiteboard, and `append` used to succeed while
writing text no Excalidraw client ever reads.

```bash
python3 $P --kind board read  '!room:server'          # FIRST: what is already there, and where
python3 $P --kind board --json read '!room:server'    # …with x/y/width/height, to find free space
# A labelled box below whatever occupied y ≤ 400 — a box and its label are two elements:
python3 $P --kind board draw '!room:server' '[
  {"id":"w1","type":"rectangle","x":40,"y":460,"width":220,"height":80,"version":1},
  {"id":"w1t","type":"text","x":56,"y":488,"width":188,"height":24,"version":1,
   "text":"Worker 1","fontSize":20,"fontFamily":1,"textAlign":"left","verticalAlign":"top"}]'
python3 $P --kind board erase '!room:server' 'w1'     # marks isDeleted, the editor's own deletion
python3 $P --kind board peers '!room:server'          # presence is its own channel — works on any kind
```

**Where a drawing lands.** The board is usually not empty, and a drawing that
lands on what is already there is unreadable together with it — two agents
that both drew "at (0, 0)" produced two complete diagrams on top of each
other. So `draw` looks first: if any element you send would overlap a live
element someone else wrote, the whole batch is moved **below** the occupied
space (only `y` changes, the batch keeps its shape). Coordinates that already
sit in clear space are written exactly as given, and re-writing your own
elements (same ids, higher `version`) never moves them. Pass `--absolute` when
the coordinates are final and you mean to draw over something. `read` first
if you want to choose the spot yourself. Use ids of your own (a prefix that is
yours) — a write to an existing id is an edit of that element, not a new one.

An element needs `id` (equal to its key), a `type` the board draws, finite
`x`/`y`/`width`/`height`/`version`. Everything else the editor reads —
`groupIds`, stroke and fill, `seed`, `boundElements` — is filled in on write with
the editor's own defaults, so a minimal element is selectable, not just drawn. A write lands only when it is **newer**
(higher `version`, ties broken on `versionNonce`), the same rule the web client
uses, so an agent and a person editing one board converge. Invalid elements are
refused rather than written — the web client validates on read, so a bad one
would be dropped by every viewer with no error anywhere.

Concurrent writes to one element are merged by Yjs on **client id**, which knows
nothing about element versions — so the older version can win and a shape
silently reverts. `put_elements` therefore arms an observer that re-asserts what
this session wrote whenever a remote change lands on it; `reconcile()` is there
for the rare case you want it by hand.

## The kanban is the third surface

A room's board of cards — `?kind=kanban` — holds two maps: `columns` and
`cards`. When a person assigns you a card, the message you receive already
says what to run; this is what it does:

```bash
python3 $P --kind kanban read   '!room:server'                       # every column, its cards, who holds them
python3 $P --kind kanban add    '!room:server' 'write the tests'      # into todo, unassigned
python3 $P --kind kanban add    '!room:server' 'review #12' --column doing --assign '@you:server'
python3 $P --kind kanban move   '!room:server' card-1a2b done          # done is a column, not a flag
python3 $P --kind kanban assign '!room:server' card-1a2b '@you:server' # '' for nobody
python3 $P --kind kanban erase  '!room:server' card-1a2b               # a tombstone, never a removal
python3 $P --kind kanban watch  '!room:server' --for '@you:server'     # assigned / moved / unassigned, as they happen
```

Taking a card is `move … doing` (or whatever the column is called — `read`
shows the ids). Finishing it is `move … done`. A fresh board has no columns
until someone opens it; `add` seeds the panel's own three (`todo`, `doing`,
`done`) so both sides agree which is which.

What the panel enforces, silently: a card must carry **all** of `id`,
`column`, `order`, `text`, `assignee`, `updated`, `by` — a card missing one is
not refused, it is *filtered out* and never appears. `assignee` is an mxid
(`@x:server`) or `''` for nobody; a display name is stored but dispatches
nobody. `order` and `updated` are integers. `by` is the writer's mxid and must
be the same string on every write — the panel tie-breaks concurrent writes on
it. This client refuses anything the panel would drop, and signs writes with
`--user-id` or `$AG2SPACE_USER_ID`.

Two writers moving one card: the later `updated` wins, ties break on `by`, the
same rule as the panel — so a move you make is a newer version, and an older
one you re-send writes nothing. A card whose column no longer exists is shown
by both sides under "no column", not lost.

## The HTML page is a fourth surface

`--kind html` is one self-contained web page the room renders live beside its
source: slides, UI mockups, landing pages, dashboards. It is text, so `read`,
`append` and `replace` work as on the Doc, sent as deltas so a person typing
alongside loses nothing.

```bash
python3 $P --kind html templates '!room:server'                   # the library, and what a page can do
python3 $P --kind html templates '!room:server' --use slides-swiss-modern   # start an empty page from one
python3 $P --kind html templates '!room:server' --use dashboard --replace    # overwrite a page, for everyone
python3 $P --kind html read '!room:server'
```

Stay inside the scope `templates` prints: HTML, CSS and JavaScript, inline or
from the CDNs it lists (reveal.js, Tailwind, React via esm.sh, D3, Chart.js…).
The preview has no fetch, form posts or storage, so data lives in the page. Several screens in one file (sections shown
and hidden by JS) stand in for several pages. People comment by pinning a point
on the rendered page; the comment's quote names what was under it.

### Presenting it: highlights, and a relay for voice

`highlight <room> <topic>` lights a `data-topic` on the page for everyone
watching (`clear` removes it). A talk deck written for a local highlight server
polls `/state`; the page answers that from the room, so the deck runs unchanged.

A voice agent needs sub-second calls, and a one-shot command re-opens the page
each time. Run the relay instead: it holds the page open and serves the local
talk-highlight API on 127.0.0.1 (`POST /highlight/<topic>`, `/speaking/on|off`,
`GET /state`):

```bash
python3 $P --kind html relay '!room:server' --port 7877
```

Point the voice tool's highlight URL at it. It binds to this machine only:
whoever reaches the port drives the stage as this agent.

The room on the command line is only the first one held. `POST /room/<id>`
(url-encoded `!abc:server`) makes the relay drop that room and hold another —
its page, and its Doc for `/script`; `GET /state` names the room held, and
`GET /rooms` lists the agent's joined rooms with names (through
`agent-room-ops rooms`, which must be installed beside this skill).
The voice tool `room_use` switches by name or id ("present in the Qingyun Group
room") and says whether that room has a page. On a voice agent that exposes the
session's docked room (`getVoiceSessionOrigin`), the tools also follow the room
the owner moves to before acting; a room picked with `room_use` holds until then.
Older hosts skip the following silently.

The skill's own voice tools use the relay: `room_slide` (next / previous / go to),
`room_highlight`, `room_point`, `room_outline`, `room_stage`, `room_surface` and
`room_script`. The last one loads a **talk
script** from the room's Doc. Under a heading "Talk script", each paragraph is a
step, and bracketed cues fire where they stand:

```text
…and that closes the loop. [next] Here is what we learned. [highlight: trust]
```

The cues are `[next]`, `[prev]`, `[slide 5]`, `[highlight: topic]`, `[clear]` and
`[pause 2]`; other brackets stay part of the words. To check one:
`python3 $P script '!room:server'`. Keep the script in the Doc so people can
review the words and cues before the talk.

### The same moves on the whiteboard and the Doc

The board and the Doc keep a `stage` map too, with the page's `nav` and `spot`
shapes, so one set of verbs drives all three. On the **board** a slide is a
frame, numbered in the board's own Present order (rows top to bottom, each row
left to right): a move changes the slide of anyone presenting, and pans everyone
else to that frame; `spot` selects and zooms to the shape or frame whose words
match. On the **Doc** a slide is a `#` heading outside code fences: a move
scrolls to it, and `spot` scrolls to and flashes the passage. Nothing is edited,
and as on the page, a surface opened later does not replay earlier moves. Topic
highlights (`/highlight`) stay page-only.

The relay holds one surface at a time: `POST /surface/board` (or `doc`, `html`)
switches, `GET /surface` names it, and `/slide`, `/spot`, `/outline`, `/state`
then act on it. `GET /outline` lists the board's frames (number, name, texts) or
the Doc's headings (number, level, title), so a "go to 3" lands where the
viewers see 3. The voice tool `room_surface` makes the switch. From the command
line: `python3 $P --kind board slide '!room:server' 2`.

### Pages that remember: the artifact runtime

A page's own scripts get `window.artifact`: `artifact.state.get / set / keys / on` for
shared state (saved in the room; a late joiner gets all of it), `artifact.emit / on`
for one-shot events (only viewers online at that moment see one), and `artifact.me`
(a stable per-viewer id and name). The host checks every request: keys are
`[A-Za-z0-9._-]` up to 64 characters, values are JSON up to 4 KB, there are at most 500
keys, and events are limited to 20 per second. The page never gets network or storage.
An agent reads and writes the same state:

```bash
python3 $P --kind html state '!room:server'                  # every key
python3 $P --kind html state '!room:server' votes            # one key
python3 $P --kind html state '!room:server' votes '{}'       # set (JSON); `null` deletes
```

The Library's **Live poll** is a worked example.

### Writing a good page

Condensed from `html-artifacts` (Apache-2.0) and `effective-html` (MIT):

- **Choose HTML only when the page earns it**: options side by side, a diagram
  or timeline, data or a chart, something to try (a slider, a flow), or a page
  people will share. For a few paragraphs, use the Doc.
- **Start from a template** (`templates`) that matches the form, and keep its
  scope: data lives in the page, since it has no fetch and no storage.
- **Readable in five seconds**: a heading and a one-line framing before any
  detail. Lay it out for real: a comparison gets columns, a sequence gets drawn.
  Headings and paragraphs alone should have been the Doc.
- **Real content, never filler**: no placeholder statistics and no controls that
  do nothing. Check any number the page states.
- **Tasteful and specific**: 60–75 characters per line, and colour only where it
  carries meaning. Avoid generic AI looks such as purple gradients on white or
  card grids for their own sake. If the design would suit a neighbouring topic
  just as well, it is too generic.
- **Works for everyone**: readable at phone width, semantic elements, controls
  that work from the keyboard, visible focus.
- **Check it before you announce it**: render it, then look at it wide and
  narrow. Exercise the controls, read the console, and fix what you see. People
  review the page by pinning comments on it; answer each one in its thread.

## The sheet is a fifth surface

`--kind sheet` is a shared grid with formulas. Rows and columns have stable ids,
so an address like `B4` is resolved when you write, and your edit lands where B4
is now, even after someone inserts a row.

```bash
python3 $P --kind sheet read '!room:server'                       # inputs as CSV (formulas as typed)
python3 $P --kind sheet read '!room:server' --json                # {"B4": "=SUM(B1:B3)", ...}
python3 $P --kind sheet set '!room:server' B4 '=SUM(B1:B3)'       # one cell: a value or =formula
python3 $P --kind sheet import '!room:server' data.csv --at A1    # a block; the grid grows to fit
```

Formulas: `+ - * / ^ &`, comparisons, ranges, and SUM, AVERAGE, MIN, MAX,
COUNT, COUNTA, IF, AND, OR, NOT, ROUND, ABS, CONCAT, LEN, UPPER and LOWER.
The web client computes the values; `read` returns what was typed.

## Databases are a sixth surface

`--kind db` holds every database in the room: typed properties, rows, and views
(table, board, calendar, list, gallery) over the same rows. The model is shared
with the web client — see `DATABASE.md`. Values are set by **property name**:
options by name, persons by mxid (comma-separated), dates `YYYY-MM-DD` (`A..B`
for a range), `Prop=` to clear. A value that does not fit is refused with the
allowed options named, and nothing is written.

```bash
python3 $P --kind db dbs '!room:server'                                        # the databases, their views
python3 $P --kind db create '!room:server' --template tasks --name Launch      # tasks|meetings|demo_day|wiki
python3 $P --kind db read '!room:server' --db Launch --view Board [--json]     # a view's rows; a board by group
python3 $P --kind db add '!room:server' --db Launch --set 'Name=Write the demo' \
    --set 'Status=In progress' --set 'Assignee=@mark:server' --set 'Due=2026-09-25'
python3 $P --kind db update '!room:server' --db Launch --row 'Write the demo' --set 'Priority=High'
python3 $P --kind db move '!room:server' --db Launch --row 'Write the demo' --to Done   # a board move
python3 $P --kind db import '!room:server' --db Launch rows.csv                # headers = property names
```

`--db` may be left out when the room has one database; `--row` is a row id or
its title. A CSV's headers map to properties case-blind; `--map 'CSV header=Property'`
names the rest (a unique header prefix is enough), `--header-row N` skips notes
above the headers, and `--year` completes dates like `Sep 25`. Unmatched headers
are reported, never created. A Google Sheet exported as CSV becomes a database:

```bash
python3 $P --kind db create '!room:server' --template demo_day --from-csv sheet.csv \
    --header-row 2 --year 2026 --map 'Team Demo Date=Demo date' --map 'Persenter=Presenter' \
    --map 'Use case brief=Use case' --map 'Time needed=Minutes' --map 'Killer Use Case Status=Killer use case'
```

By voice: `room_db_list`, `room_db_read`, `room_db_add`, `room_db_update` and
`room_db_move` go through the relay's `/db` routes (the relay needs `--user-id`
to sign writes). They work whichever surface the relay holds; `POST /surface/db`
holds the databases open for faster calls.

## Collaborating, rather than submitting

For anything beyond one edit, import the library and **hold the connection**:

```python
from room_collab_client import open_room_collab

async with open_room_collab(url, room_id, token) as doc:
    await doc.set_presence("mars")       # otherwise you edit invisibly
    print(doc.text, doc.peers)
    await doc.append("...")
    await doc.replace("old sentence", "new sentence")
```

Three things that matter more than they look:

1. **Publish presence.** Without it you are editing a document where nobody can see
   you — the person sharing it sees text appear from nowhere. Pass `user_id`
   (this agent's mxid) to get an avatar: the roster resolves faces by id, never
   by display name, so without it you appear by name with no face. Each
   `append`/`insert`/`replace` also places your caret at the write's end, so
   the editor draws where you last wrote, in your colour, like a person's.
2. **One connection per agent per surface.** Each connection is a separate peer:
   open a new one per edit and you appear in the presence list several times, as
   several people. Hold the context manager open instead.
3. **Send deltas, not the document.** `append`/`insert`/`replace` put only the change
   on the wire, which is why a human typing in the same paragraph loses nothing.
   Rewriting the whole text would be a last-writer-wins overwrite.

**Before you design where something is stored, read
[`CRDT-SHAPES.md`](CRDT-SHAPES.md).** It is the measured answer to which
arrangements merge and which silently drop a write — many text roots, one map
key per row, an order derived from `(created, id)` rather than stored. The
failure it describes does not look like a failure: a row that was written is
simply not in the list, with no error and no gap, and nobody notices an absence
they were never shown. Two of us each lost an evening to a premise we had
stated as a structural constraint without measuring it; the discriminator was
ten lines both times.

## Who wrote what

```bash
python3 $P --with-authors read '!room:server'
```

Prints, above the text, which Yjs client id belongs to which account and
whether it is a person or an agent (and whose agent). The document records
this on the server as writes land; an agent cannot claim authorship, only
read it (verified 2026-09-20). Use it to decide whether a paragraph is a human's to leave alone or
another agent's to continue.

## Commenting on a passage, rather than editing it

When something a person wrote is unclear, ask about it *there* instead of
rewriting it or asking in the timeline where the words are out of sight:

```bash
python3 $P comment '!room:server' 'option A is cheap' 'cheap in money, or in time?' --mention '@qingyun:server'
python3 $P comment '!room:server' 'option A is cheap' '…' --nth 1     # the second occurrence
python3 $P comment '!room:server' 'option A is cheap' '…' --dry-run   # show the message, post nothing
```

The quote must be the exact words as they stand in the document (up to 2000
characters), and it must be unique — or say which occurrence with `--nth`
(0 is the first). The command refuses rather than guessing. What it posts is an
ordinary room message — `> the quoted words`, a blank line, your text — carrying
the anchor a web client pins the comment to, so the person sees it beside the
passage and anyone in a plain client still reads it as a sentence. `--mention`
writes the mxid into the text, which is what makes it a real mention; an agent
among them is called.

Posting goes through the `agent-room-ops` skill installed beside this one
(`room_ops.py say --extra-content`); without it the command says so, and
`--dry-run` gives you the exact message to post another way.

A comment is a thread. To answer in it — yours or a person's — reply under the
comment's event id (the receipt's `event_id`, or the id shown in the room):

```bash
python3 $P reply '!room:server' '$eventid' 'in time — the build is the slow part' --mention '@qingyun:server'
```

The body is your words as they are (no quote in front: the thread already says
what it is about, and the client shows a reply verbatim). It is a room message
with the thread relation the web client reads replies from, so it appears under
the comment beside the passage; no document connection is opened for it.

## What a refusal means

The service **accepts the socket and only then closes** with a code, because a
close before accept cannot carry one. So these arrive as a failure to open, not
as an HTTP status:

| Close | Meaning |
|---|---|
| 4400 | The room id is malformed. **Not** "a room that exists and is empty". (verified 2026-09-20) |
| 4404 | The surface kind is malformed. (verified 2026-09-20) |
| 4403 | Refused or withdrawn: not authorized for documents, or membership/write power changed. It can *also* mean core-api was briefly unreachable, so one 4403 is not proof of revocation. (verified 2026-09-20) |
| HTTP 401 "bearer is not a valid Matrix user session" | The service has no record of this agent's token — a provisioning gap on that deployment (the local rig, typically), not a room permission. A different problem from 4403; ask whoever runs that deployment. (verified 2026-09-20) |

Each row carries the date it was last measured against the service, and
`tests/room-collab-skill-claims-expire.test.py` fails once a row is older than
30 days: a sentence about what the service refuses is an observation with a
shelf life, not a rule. Re-measure and move the date; do not delete the date.

A refusal is raised, never returned as an empty document — if it were, "this
room does not exist" and "this document has no content" would look identical.
