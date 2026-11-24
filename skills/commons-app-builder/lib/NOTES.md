# Notes: choices made, and what a real room must confirm

"The guide" is the Commons app guide ([`GUIDE.md`](../GUIDE.md)). It is this repo's reference for
the page API, which the host owns. The library uses only what the guide documents. Nothing here
was run in a real room.

## Where the guide was silent or unclear, and what the library does

| # | Question | Choice |
| --- | --- | --- |
| 1 | Should `room.map` sit on `artifact.state` or on rows? | Rows. The guide names `state.get/set/delete/keys/all/on` but gives no return or callback shapes, and no agent Action reads page state. Rows are documented in full and an agent can read and write them. |
| 2 | How to delete a row from a page? | Not possible: the guide has no page call for it. `map.delete` empties the value cell. `list.remove` writes a marker (default text column `Removed` = `"yes"`, or any column and value the app names, such as `State` = `Cancelled`). |
| 3 | What value does a checkbox cell take? | Not stated. The default "removed" marker is therefore a text cell, not a checkbox. |
| 4 | What is the conflict rule for two writes to one cell? | Not stated. The library assumes the last write to reach the room wins and documents that. It adds no rule of its own on top. |
| 5 | Two pages create the same map key at once. | The bridge has no unique constraint, so two rows result. Every page uses the row with the lowest id (plain string compare). This is arbitrary but the same on every page. |
| 6 | In what order does `db.read` return rows? | Not stated. The list keeps the bridge's order as given. `orderBy` sorts by a column for apps that need a defined order. |
| 7 | Does a page get `onChange` for its own write? | The guide's pattern ("re-render from each snapshot") suggests yes, but does not say so. The library does not rely on it: after a successful write it keeps the value in its own copy. If a snapshot arrived while the write was in flight, the snapshot is trusted instead. |
| 8 | Optimistic apply or write-then-refresh? | Optimistic with rollback for writes to existing rows (`db.write` never rejects and reports `{ok: false, reason}`, so rollback is exact). `push` waits for the room, because a locally invented row id could show the row twice. |
| 9 | What do the 20,000 bytes measure? | Not stated. The library measures the JSON of the value (for `push`, of the whole cells object) and refuses above 20,000 before sending. |
| 10 | Refusal reasons are free text. | The library maps the phrases the guide quotes (`waiting for the viewer`, `not allowed by the viewer`, `page permission required`, `rate limit`, `too many`, `declined`, and the `sendTask`/`post` reasons) to codes. Anything else is `REFUSED` with the text in `reason`. |
| 11 | Should the library retry? | Yes, as the guide's `save` loop does: while the viewer has not answered or the page is rate-limited, every 1.5 s for up to 2 minutes. Both are options of `connect`. |
| 12 | How to settle a claim race? | One text cell holds the whole claim, so a claim is a single write. Where the bridge gives a `version` and takes `{ifVersion}`, the write is conditional and the room settles the race (`certainty: "room"`). Elsewhere the write is plain, the result is `certainty: "local"`, and a loss is reported by `onLost` when the copies meet. There is no read-back after a plain write: the page's read answers from its own copy and would only show its own claim. An append-only claims table was considered and dropped: it needs a trusted order. |
| 13 | Who is the holder in a claim? | What the claimer wrote: `by`, `name`, `kind`. It is not attested. The guide warns that a writable field is never proof of identity; the README repeats it. |
| 14 | What clock ends a claim? | `artifact.now()` (estimated server time), and the device clock while that is `NaN`. |
| 15 | `agent.list()` returns `kind`. Kind of what? | Not stated. The library drops it and returns `kind: "agent"`, plus `inRoom` from `userId`. |
| 16 | Presence ("who is viewing")? | The guide documents `members()` only: joined members, not viewers. `players()` is membership and says so. |
| 17 | Live events (`emit`/`on`)? | Left out: the callback's arguments are not documented. |
| 18 | Replies from an agent? | The guide says a page gets none back. `ask` and `post` return the dispatch receipt only. No reply reading is offered. |
| 19 | Named-agent requests. | One isolated adapter (`requestsAdapter`), behind feature detection of all three calls, marked experimental. It passes the request list through as the design gives it (including `state` and `outcome`); if that part of the design changes, only this function changes. |
| 20 | Unsubscribing an experimental `requests` watch. | The design shows no way. `watch` returns none. |
| 21 | One watch per database. | All handles on one database share one watch and one write queue. What the bridge does on a second watch is not stated; the fake throws, to catch misuse. |
| 22 | `db.list()`: does it count as a read? | Not stated. The library calls it once per `map()` or `list()` call. |
| 23 | Versions and conditional writes. | The guide does not describe them. The source is `docs/commons-html-page-api.md` in the web client (cinny-webclient) repository, section "Database versions and conditional writes": the number in `version` on what `db.read` and `db.onChange` give, `{ifVersion}` as the last argument of `db.write` and `db.createRow`, and the refusal reasons. That document owns the contract and it is not copied here. The library feature-detects: a missing `version` means the plain path. |
| 24 | Which version is the basis for the next conditional write? | The one the room's answer carries (`{ok: true, version}`); the page API document says it is there "when the server returns it", so both shapes are handled. With it, the library moves its basis forward at once and keeps the write on top of its snapshot until a snapshot that new arrives. Without it, the basis stays where it was and the next conditional write waits for the page's copy to catch up. |
| 25 | Reasons the guide does not quote. | "This write is waiting for approval", "offline" and "no answer from the room; read again before deciding" are worded in that same section, for conditional writes. There they get the codes `APPROVAL_PENDING`, `OFFLINE` and, for no answer, the handling in choice 26. For plain writes none of them has a code: they come back as `REFUSED` with the text. |
| 26 | A conditional write with no answer. | The outcome is unknown and the document names no key to resend it and no receipt. `map.update` rejects with `OUTCOME_UNKNOWN` and what the page sees; it never infers success from an equal value and never reruns the function. A claim goes on, because its token is its own proof. |

## Orderings: what the library does

A write has an answer; the room also sends snapshots; and some calls show their change early.
These can arrive in any order. "Conditional" means `claim`, `release` and `map.update` where a
snapshot carries a `version`; everything else is a plain write.

| Operation | Answer before the snapshot | Answer after a newer snapshot | No answer | Same viewer's other tab acts in between | Retried |
| --- | --- | --- | --- | --- | --- |
| `map.set`, `map.delete`, `list.update`, `list.remove` (plain) | The value is kept on the page's copy | The snapshot is trusted; the answer adds nothing | Cannot happen: the host queues a plain write | Last write to reach the room wins | Only while the viewer has not answered or the page is rate-limited; nothing was written before |
| `list.push`, a new key in `map.set` (plain create) | The row is added | The snapshot is trusted: a row it lacks is not added back | Cannot happen | Two rows; for a map key the lowest id is used | As above |
| `map.update` (conditional) | Shown on top of the snapshot until a snapshot at the answer's version or later arrives | Adds nothing: that snapshot has the write, or what became of it | Rejects `OUTCOME_UNKNOWN`; no guess from the value, `fn` not run again | The other tab's write changes the version: read again, run `fn` on the new value | Only after a definite "version changed" (five tries, then `CONTENDED`) |
| `claim`, renew (conditional) | As `map.update`; the result is `certainty: "room"` | As `map.update` | Read again; a token this call wrote in the cell is proof; else try again; else `reason: "unknown"` | A claim by the other tab is a different token: this tab's `onLost` is called | Each try writes a new token and a new expiry |
| `release` (conditional) | The cell shows empty | As `map.update` | Read again; empty means released; the pinned claim still there is tried again; anything else is left alone; else rejects `OUTCOME_UNKNOWN` | A renewal by the other tab is not the pinned claim: left alone, resolves `false` | Only against the pinned claim |
| `claim`, renew, `release` (plain) | Kept on the page's copy; `certainty: "local"` | The snapshot is trusted | Cannot happen | Not visible until the copies meet; a plain release can clear a claim it has not seen | As the first row |

Rules that hold for every row:

- A snapshot older than the last one is dropped, so the version a page builds on never goes back.
- A snapshot without a `version`, after ones that had it, is taken as it is: acknowledged writes
  on top of it are dropped and the page writes plainly until a version returns.
- An answer cannot arrive twice: each call has one answer.
- While a claim or release is in flight, the page does not judge that row's claim as lost.

`test/interleavings.test.js` runs 300 seeded orderings of answers and snapshots across two viewers
and a second tab of the first, and checks: no page shows a row or value the room lacks once the
newest snapshot is in; versions never go back or ahead of the room; a counter ends between its
definite successes and those plus its unknown outcomes; a claim called `"room"` is one the room
took, over nobody else's live claim; a conditional release clears only the claim it was pinned
to; a page tracks no claim the room does not hold.

## Real-room checklist

Each item is an assumption. Check it with two people (and one agent) in a real room.

1. After a page's own successful `db.write` or `db.createRow`, its `onChange` fires with a snapshot that includes the write.
2. A snapshot delivered after a write resolved never predates that write. (If it can, a value flickers back for a moment.)
3. Two pages write different columns of one row at once: both values survive.
4. Two pages write the same cell at once: the later arrival wins, both get `{ok: true}`, and both converge.
5. `db.read` returns rows in a stable order, and new rows come last.
6. Row ids are unique strings that never change, on every page.
7. A text cell stores and returns a JSON string unchanged, up to the write limit.
8. The 20,000-byte limit measures roughly the JSON of the value; note the reason text when it is exceeded.
9. Writing `""` to a text cell clears it; note what reads back (`""`, `null` or a missing key). The library treats all three as empty.
10. The rate-limit reason matches `rate limit` or `too many`, for writes. Note the text for reads (`db.read` rejects).
11. Whether a refused write counts against the 30 a minute, and whether `db.list()` counts as a read.
12. A select or status cell written from a page takes the option name, and a wrong name gives `{ok: false, reason}`.
13. `db.createRow` with an unknown column refuses the whole row.
14. The `created_by` cell is filled on rows a page creates, and `personId` resolves it to the same id as `me.id` for the author. Check in the deployment you ship to: stamping may be switched off.
15. `members()` and `personId()` ask the access question themselves, or only reject until a write has asked it.
16. The consent reason contains `waiting for the viewer` (the guide prints it with a typographic apostrophe after "viewer").
17. A second `db.onChange` on one database: what happens. And that the returned function frees the watch.
18. `onChange` delivers `null` when the database is removed, and later writes resolve `{ok: false}` rather than hang.
19. `artifact.now()` on two devices agrees within a second or two. Claim expiry depends on it.
20. `me.kind` can be `"agent"` on a page, and how an agent that only uses Actions learns an id to put in a claim's `by` (any stable string works for the claim rule; matching it to `players()` needs the per-room id).
21. An agent writing the `Claim`, `Status` or `Result` cell through Actions does not trigger an unwanted summon on that row.
22. `agent.sendTask` with a repeated `key` returns the first receipt and shows no second dialog.
23. `room.post` and `agent.sendTask` reasons match the guide's wording exactly.
24. The inlined library (about 35 KB) plus a page stays well under the 196,608-byte page limit, and runs in the sandbox with no blocked call. (The guide's static pre-flight, `check_page.py`, passes on both examples.)
25. Each "version changed" answer costs one `db.read`; check that is affordable in a busy app: 20 reads a minute per page.
26. Phone layout of the two examples at about 390px, light and dark. Not rendered here: the checks run without a browser.
27. The s.space deployment you ship to has versions and conditional writes turned on: `db.read` and `db.onChange` snapshots carry a number in `version`. This is a deployment setting, not a per-room one; a page still feature-detects on each read.
28. With them on, two pages claim one row at the same moment: one gets `certainty: "room"`, the other `reason: "held"`, and neither page shows a false `onLost` afterwards.
29. After an accepted conditional write, when the page's own copy shows it under real sync, and whether a snapshot older than it can still arrive. (The library ignores such a snapshot by its version.)
30. With them off, two pages claim one row with plain writes: both get `certainty: "local"`, and `onLost` fires on one of them after they sync. Note how long that takes on a slow connection.
31. After "no answer from the room" on a conditional write, how often the write had in fact landed, and that the read which follows shows it.
32. A plain write made while the page is offline or waiting for an approval: whether it stays in the page's own copy. (The library reports `REFUSED` and undoes its own early display.)
33. A snapshot's rows can be ahead of its `version` for a moment (the document says the version lands just after the change it counts). Check that a conditional write based on such a snapshot is refused with "version changed", and that the next snapshot carries the new version.
34. Two tabs of one viewer: the same `me.id` in both, and a claim from one calls `onLost` in the other.

## Not tested here

- No real room, no real consent prompt, no second device, no real agent.
- No browser: the example checks run the pages' scripts in Node with a small stand-in DOM. Layout and CSS were not rendered.
- The fake room's reason texts for size, bad values and read limits are its own; only the phrases the guide quotes are faithful.
- The conditional-write path and the "page's own copy" behaviour are tested only against the fake's model of them, which was written from the page API document, not checked against a running host.
