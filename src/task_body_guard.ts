/**
 * confineUserContent — the ONE TypeScript implementation of the task-body
 * injection guard. TS mirror of src/task_body_guard.py:confine_user_content().
 *
 * Extracted because the guard existed as two hand-copies (task-bridge.ts and
 * the phone skill's conversation-server.ts) and a third writer arrived without
 * one — src/voice-host.ts interpolated a model-supplied `task:` unguarded.
 * Copies drift: conversation-server's own comment records that main's version
 * had already "dropped the wrapping" once.
 */

const _ZWSP = '​';
// Kept in lockstep with local_task_protocol.KNOWN_HEADER_KEYS (the Python
// guard's source of truth). TS can't import the Python tuple, so this list is
// the mirror; injection-guard-sweep asserts parity so drift fails CI. Synced to
// the full 38-key set on the 2026-07-13 main merge (main widened the Python side
// from 14 → 38; the TS guard must defang the same keys or forged interaction_type:
// / attachments: / media_form: lines slip through here).
const _HEADER_KEYS = [
	'id', 'timestamp', 'session_scope', 'task', 'source', 'access_tier', 'user_id',
	'channel_id', 'priority', 'interaction_type', 'source_message_id',
	'channel_name', 'guild_name', 'attempts', 'sender_name', 'room_name',
	'parent_message_id', 'reply_chain_ids', 'reminder', 'author_name', 'author_id', 'chat_id',
	'thread_ts', 'reply_to_event', 'reply_to_me', 'reply_to_sender', 'addressed_to', 'callSid', 'caller',
	'receiving_instance',
	'from', 'call_sid', 'hint', 'instructions', 'transcript',
	'schedule_name', 'schedule_slot',
	'content_modalities', 'media_form', 'attachments', 'platform_card',
	'instance_id',
];
const _HEADER_RE = new RegExp(`^(?:${_HEADER_KEYS.join('|')})\\s*:`, 'i');
const _FENCE_RE = /^={3,}/;
// Every separator str.splitlines() / universal-newline readers treat as a
// line boundary — fold ALL to '\n' so the guard's line-set matches the
// reader's (else \v \f \x1c-\x1e \x85 \u2028 \u2029 smuggle a forged line past it).
const _LINE_SEP_RE = /\r\n|[\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/g;

/**
 * Defang user-supplied content before embedding in a task file.
 *
 * Prefixes any line that looks like a task-file header field or a
 * ===FENCE=== with U+200B so structural injection (access_tier forge,
 * system-instruction fence) cannot succeed. Idempotent; folds every str.splitlines() separator (not just CR/CRLF).
 * TypeScript mirror of src/task_body_guard.py:confine_user_content().
 */
export function confineUserContent(text: string): string {
	if (!text) return text;
	const normalized = text.replace(_LINE_SEP_RE, '\n');
	return normalized.split('\n').map(line => {
		const probe = line.trimStart();
		if ((_HEADER_RE.test(probe) || _FENCE_RE.test(probe)) && !line.startsWith(_ZWSP)) {
			return _ZWSP + line;
		}
		return line;
	}).join('\n');
}
