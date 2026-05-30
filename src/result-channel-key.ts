/**
 * Per-channel pull path for task-result files in `results/`.
 *
 * REGULAR task results stay at `results/task-{id}.txt` — the default. The
 * existing consumers (discord-bridge / telegram-bridge / slack-bridge /
 * task-bridge / agent-api) all key off that name (specific task_id or
 * `task-*` glob) and are NOT modified by this scoping.
 *
 * NEW namespace — `results/<channel-key>.task-{id}.txt` — is used ONLY
 * when a task result needs to reach a non-delegating pull consumer (today:
 * discord-voice and phone). A `.`-prefixed filename slides past the
 * existing consumers' patterns because none of their startsWith / glob /
 * pending-id lookups match the channel-key prefix.
 *
 * Channel keys we emit:
 *   - discord-voice → Discord voice channel id (CHANNEL_ID arg / env)
 *   - phone         → Twilio call SID (per-call unique)
 *
 * Twin of src/result_channel_key.py — keep in sync if a Python writer is
 * added.
 */

// Filename-safe alphabet. Any char outside it is collapsed to `-` so a
// stray channel id can never inject a path separator or a regex special.
const KEY_SAFE_RE = /[^A-Za-z0-9_-]/g;

// Scoped filename shape: `<channel-key>.task-{id}` (with or without .txt).
// The key must NOT contain `.` (so `task-foo.txt` itself never matches).
const SCOPED_RESULT_RE = /^([A-Za-z0-9_-]+)\.(task-.+)$/;

/**
 * Collapse `raw` to the filename-safe key alphabet. Empty / falsy input →
 * `'unknown'` sentinel so the produced filename always has a non-empty
 * prefix and stays distinct from the legacy `task-...` form.
 */
export function sanitizeKey(raw: string | null | undefined): string {
	if (!raw) return 'unknown';
	const cleaned = String(raw).trim().replace(KEY_SAFE_RE, '-');
	return cleaned || 'unknown';
}

/**
 * Build the scoped task-result filename for a given channel + task id.
 * Returns `<channel-key>.<task-id>.txt`.
 */
export function resultFilename(channelKey: string, taskId: string): string {
	return `${sanitizeKey(channelKey)}.${taskId}.txt`;
}

/**
 * Parse a filename in `results/`. Returns `[channelKey, taskId]` for the
 * scoped form (`<key>.task-{id}[.txt]`), and `[null, base]` for anything
 * else (legacy flat `task-{id}.txt`, `voice-...`, `proactive-...`, etc).
 * The `.txt` suffix is optional on input.
 */
export function parseResultFilename(filename: string): [string | null, string] {
	const name = filename.endsWith('.txt') ? filename.slice(0, -4) : filename;
	const m = SCOPED_RESULT_RE.exec(name);
	if (m) return [m[1], m[2]];
	return [null, name];
}

/**
 * True iff a result `filename` is the scoped form claimed by `channelKey`.
 * Legacy flat `task-{id}.txt` files return false — they're owned by their
 * delegating consumer (discord-bridge / task-bridge / etc), NOT by a
 * per-channel scan.
 *
 * Requires an EXACT `.txt` suffix. Atomic-write temps like
 * `<key>.task-X.txt.tmp`, `.sending`, `.partial` etc. must NOT match —
 * reading/unlinking a writer's in-flight temp before the rename completes
 * would inject a half-written body and orphan the rename target. The scan
 * loops also gate on `.endsWith('.txt')` as belt-and-suspenders.
 */
export function resultBelongsTo(filename: string, channelKey: string): boolean {
	if (!filename.endsWith('.txt')) return false;
	const [key, taskId] = parseResultFilename(filename);
	if (key === null) return false;
	if (!taskId.startsWith('task-')) return false;
	return key === sanitizeKey(channelKey);
}

// ── Typed channel-key constructors ────────────────────────────────────────
// Per-consumer prefixes make key-namespace collisions impossible across
// future consumer types: a hypothetical new pull surface whose ID format
// happens to be pure digits (like a Discord VC snowflake) cannot accidentally
// claim a `dvoice-*` filename. Writer and consumer MUST go through the same
// constructor so the keys agree. Keep symmetric with `src/result_channel_key.py`.

/** Channel key for the discord-voice consumer. Wraps a Discord voice-channel id. */
export function discordVoiceKey(vcId: string | null | undefined): string {
	return `dvoice-${sanitizeKey(vcId)}`;
}

/** Channel key for the phone-conversation consumer. Wraps a Twilio call SID. */
export function phoneCallKey(callSid: string | null | undefined): string {
	return `phone-${sanitizeKey(callSid)}`;
}
