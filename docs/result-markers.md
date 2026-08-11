# Result-body protocol markers — full reference

Extended reference for the `CLAUDE.md` § Task bridge marker list. The rules
inline in `CLAUDE.md` are authoritative; this file carries the full per-marker
semantics, per-bridge behavior, id formats, examples, and the migration history
of the centralised parser.

## Per-marker semantics

A marker takes effect when the result body STARTS with it (except `[dm-only]`,
which is detected anywhere — see below). Use markers when multiple related
tasks should produce ONE user-facing reply instead of N separate ones.

### `[deduped: task-<other-id>]`

Both voice (task-bridge) and Discord (discord-bridge) silently archive this
task as done, no narration, no DM. Put the full reply in the other task's
result file and put this marker in each superseded task's result. The
canonical way to handle thread-consolidated replies — e.g. when voice
over-delegates 3 tasks for the same continuation utterance (see
`src/task-bridge.ts:527`).

### `[no-send]`

Discord bridge skips delivery for this task (still archives). Use when the
task is internally handled but produces no user-visible reply.

### `[REPLIED]`

Discord bridge skips delivery (already sent through another path).

### `[channel: <channel-id>]`

When this is the first non-empty line of the body, the bridge delivers the
rest of the body to `<channel-id>` instead of the originating channel (and
drops `thread_ts` since the post is moving threads). Discord ids are 17-20
digits; Slack ids match `[CDG][A-Z0-9]+`. Use when a task arrives in a noisy
channel but the reply belongs somewhere else (e.g. #dev). Telegram silently
drops it — no concept of "channels" on that surface.

### `[dm-only]`

Privacy guard: suppresses any `[channel:]` redirect on the same body
(regardless of marker order), so a body carrying private data can never be
*redirected* out to a shared channel. It marks dm-only intent but does not by
itself force a DM — that stays the consumer's job. In practice the private
producer (the morning briefing's calendar + email) is emitted as a proactive
result (`results/proactive-*.txt`), which every bridge already delivers to the
owner's DM; `[dm-only]` reinforces that by guaranteeing no stray `[channel:]`
redirect overrides it.

**Detected anywhere in the body** — that is what makes the guard undefeatable
by marker order, and over-triggering it fails safe. **Stripped only when the
marker stands alone on its line**, before delivery and before voice speaks it;
a marker mentioned inline in prose is detected but the text is delivered
verbatim. Parsed by `result_markers.parse_markers`.

### `[file: /path]` / `[send: /path]` / `[attach: /path]`

Discord bridge extracts and attaches the file alongside the text body.

## Centralised parsing — migration status and history

*Migration status: all four Python consumers conform, and the guard enforces
it.* `discord-bridge.py`, `dm-result.py`, `telegram-bridge.py`, and
`slack-bridge.py` all obtain marker grammar from `parse_markers()`
(`src/result_markers.py`), and `tests/bridge-marker-no-leak.test.py` fails if
any of them declares the grammar itself — matching the grammar in any regex
literal, so a renamed private parser cannot slip past. Telegram's
`send_reply()` used to compile its own `file|send|attach` regex and Slack
declared the same regex dead at module scope; both are gone. Add any new
consumer to that guard when it starts handling markers.

Why private copies drift: `discord-bridge.py` and `dm-result.py` each carried
a regex that only matched `/...` or `~/...` values, so a marker every other
consumer stripped was delivered to the owner as literal text. Guarded by
`tests/bridge-marker-no-leak.test.py`.

Attachment-path authorization is a separate concern owned by
`src/send_allowlist.py`, applied immediately before the upload sink. The
dependency direction is one-way:

    parse_markers()  ->  send_allowlist.is_path_sendable()  ->  transport upload
