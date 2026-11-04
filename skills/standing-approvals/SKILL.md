---
name: standing-approvals
description: Before any irreversible action you would otherwise ask the owner about (merge a PR, send an email, post publicly, spend money, delete, deploy), check whether he has a standing approval that covers it; log each use; propose a new one when he says "you don't need to ask me for X". Also when asked "what have I pre-approved?" or to revoke one.
---

# standing-approvals

A standing approval is the owner's pre-approval for a class of irreversible actions. It lives
as a row of the **Standing approvals** database in his room (his DM with the agent by default),
where he can see, edit and revoke every one. A remembered chat message is not a standing
approval; only a row that counts is.

The room is `STANDING_APPROVALS_ROOM` (`--room`, env, then this manifest's `config`), else the
owner's DM. The owner's account is `STANDING_APPROVALS_OWNER` by the same order, else the owner
the AG2 Space connection serving that room records.

```bash
S=skills/standing-approvals/scripts/standing_approvals.py
python3 $S check --kind "merge PR" --attr repo=sonichi/sutando --attr branch=main
python3 $S log-use --row <row id> --action "merged PR #123" --target sonichi/sutando --task <task id>
python3 $S propose --kind "send email" --scope "to: *@example.com" --expires 2026-12-31 \
    --note "invoices only" --granted-by <link to the owner's message>
python3 $S list [--json]
python3 $S ensure
```

## The sequence: check → act → log-use

1. **check** before the action. Exit 0 prints the covering row id first; 1 means not covered
   (with why each same-kind row did not count); 2 means the store could not be read.
2. **Anything but exit 0 means ask the owner**, exactly as you would have without this skill
   (`pending-questions`). An unreadable store is never a yes.
3. Act, then **log-use** with the row id: Uses goes up by one, Last used is stamped, and a line
   (time, action, target, task) is appended to the row's page.

Show the row's Note to the owner when you report the action; it is never part of the match.

## Which rows count

A row counts only when all of these hold:

- Status is **Active**, and the Status cell was last written by the owner's account;
- Kind, Scope and Expires were each written by the owner, or before he set it Active (an edit
  by anyone else after activation stops the row counting until he confirms it again);
- Expires is empty or still ahead (a date alone runs through the end of that day, host time);
- Scope parses.

**You may only propose.** `propose` writes Status Proposed; nothing in this skill writes Active,
and a row you mark Active yourself does not count. Tell the owner you proposed it and that he
turns it on by setting Status to Active. Granted by is a link to his message for the record; it
is shown, never trusted on its own.

## Kind and Scope

Kind is free text, matched case-insensitively and exactly ("merge PR" is not "merge PRs").
Use the owner's own wording and reuse an existing Kind rather than inventing a near-duplicate.

Scope is one condition per line (`;` also separates); every line must hold:

| line | meaning |
|---|---|
| `repo: sonichi/*` | every `repo` the action names matches the glob |
| `to: *@example.com \| *@example.org` | each `to` matches one of the globs |
| `branch != main` | no `branch` the action names matches |
| `amount <= 50` | also `<`, `>=`, `>`, `==`; each `amount` is a number in bound |
| `any` | any target for this kind (alone on its line) |

Keys are free-form (`repo`, `room`, `to`, `domain`, `path`, `branch`, `channel`, `account`,
`amount` …). Globs and keys are case-insensitive; `*` also matches `/`. **Fail closed:** a key
the scope names that the action does not supply means no match, so pass every relevant
attribute with `--attr key=value`, and repeat a key for several values (every recipient as
`--attr to=…`; every value must fit). An empty Scope means any target only on a row the owner
created himself; a proposed row must say `any`.

Examples:

```text
repo: sonichi/*
branch != main
```
```text
to: *@mycompany.com
```
```text
merchant: *aws*
amount <= 50
```
