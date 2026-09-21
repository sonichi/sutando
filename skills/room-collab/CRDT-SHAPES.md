# Shapes that merge, and shapes that lose writes

A room-collab surface is a Yjs document, and how you arrange data inside it decides
whether two people editing at once keep both their changes or silently lose one.

Everything here was measured with pycrdt, not reasoned about. Two of us spent an
evening each stating a wrong premise as a structural constraint and building on it;
the discriminator was ten lines both times.

## What merges

**Many text roots in one document.** `doc["post:1"]`, `doc["post:2"]` … each merge
independently, and concurrent edits to the same root from two peers both land.

```
a["post:1"] += "A wrote "     b["post:1"] += "B wrote "
b["post:2"] += "only B here"
after sync -> post:1 == "A wrote B wrote ", post:2 == "only B here"
```

So "one surface holds one editable thing" is **false**. A surface can hold as many
independently-collaborative texts as you like. Getting this wrong costs a feature:
it is what makes a composer able to hold a list of drafts, each with its own carets,
write-time ledger and comment anchors, rather than one privileged draft and a pile
of second-class ones.

**One key per row in a `Map`.** Three concurrent adds from two peers all survive.

**The awareness cursor carries the root NAME** (`tname`), so a caret in `post:1` is
distinguishable from one in `post:2` with no protocol change. Per-text carets fall
out of the shape for free.

## What silently loses writes

**A nested dict stored under one map key.** `c["posts"] = {id: {...}}` is a single
value, and a whole-value write is last-writer-wins whatever the value contains.

```
two peers each add a post, neither having seen the other
  posts as one nested dict -> ['1', '2']      post 3 is gone
  one key per post         -> ['1', '2', '3']
```

Note what the failure looks like from outside: a **shorter list**. No error, no gap,
nothing in the result admitting a row was dropped — and nobody notices an absence
they were never shown. Moving prose out of a string and then putting the *index* of
the prose into one value is the same defect, one level up.

**A stored `order` array**, for the same reason: two concurrent appends are two
whole-list writes and one wins. Derive the order instead. A `Y.Array` merges per
element and is what manual reordering needs — but not before someone asks for it.

## Deriving an order needs a total comparison

Sorting by a `created` timestamp alone is underspecified when two rows tie, and each
client then falls back to its own map iteration order:

```
rows: a1(created=200), b2(created=200), c3(created=100)
by created      -> client X ['c3','a1','b2']   client Y ['c3','b2','a1']   differ
by (created,id) -> client X ['c3','a1','b2']   client Y ['c3','a1','b2']   agree
```

Two people see the same feed in a different order, intermittently, which reads as a
sync bug rather than a missing tiebreak. Sort on `(created, id)`: every client then
computes the same sequence from the same data with no coordination.

`created` also carries the writing machine's clock, so across machines the order is
roughly chronological, never authoritative.

## Testing any of this

A test that asserts one expected sequence passes with the tiebreak removed. Feed the
**same rows in two different iteration orders** and compare the two results — that is
the failure a person would actually report, and it is the only form that fails for
the right reason.

## Prose belongs in a text, never in a string field

A map value holding prose is last-writer-wins: two people editing a draft, and one
paragraph disappears. It also cannot carry carets, the write-time ledger or comment
anchors, since those are all text positions. If something is going to be edited by
more than one party — and on this platform an agent is a party — it is a text root.
