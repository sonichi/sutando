// Two decisions the result-watcher used to make with one predicate:
// suppression (universal — a marked body must never be spoken or sent) and
// retirement authority (scoped — only the consumer that dispatched a task may
// archive its result). Narrowing one silently narrowed both.

export const SKIP_MARKER_RE = /^\s*\[(?:no-send|REPLIED)\]/i;

export function isSkipMarked(file: string, result: string): boolean {
	return file.startsWith('task-') && SKIP_MARKER_RE.test(result);
}

// Sources whose own bridge polls results/ and archives what it dispatched.
// Closed set: a transport is added here only when its bridge gains that loop.
// The complement — every local source — is open-ended and host-dependent, so
// enumerating IT cannot stay correct: measured on two hosts the same day, the
// missing local families did not overlap at all.
export const NETWORK_CONSUMER_SOURCES = [
	'discord', 'ag2space', 'telegram', 'slack', 'whatsapp',
];

/** Header fields a retirement decision may read. `null` = task file gone. */
export interface TaskOrigin {
	source: string | null;
}

export function hasNetworkConsumer(origin: TaskOrigin | null): boolean {
	// Unreadable origin is treated as foreign. Wrongly retiring strands an
	// owner reply; wrongly keeping only accumulates a file, and suppression
	// is universal either way — so the unknown case fails toward keeping.
	if (origin === null) return true;
	if (origin.source === null) return false;
	return NETWORK_CONSUMER_SOURCES.includes(origin.source.trim().toLowerCase());
}

export function mayRetireSkipMarked(
	file: string,
	result: string,
	isOwn: (taskId: string) => boolean,
	originOf: (taskId: string) => TaskOrigin | null,
): boolean {
	if (!isSkipMarked(file, result)) return false;
	const taskId = file.replace(/\.txt$/, '');
	// Dispatched by this bridge, or belongs to no other consumer.
	return isOwn(taskId) || !hasNetworkConsumer(originOf(taskId));
}
