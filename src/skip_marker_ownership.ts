// Two decisions the result-watcher used to make with one predicate:
// suppression (universal — a marked body must never be spoken or sent) and
// retirement authority (scoped — only the consumer that dispatched a task may
// archive its result). Narrowing one silently narrowed both.

export const SKIP_MARKER_RE = /^\s*\[(?:no-send|REPLIED)\]/i;

export function isSkipMarked(file: string, result: string): boolean {
	return file.startsWith('task-') && SKIP_MARKER_RE.test(result);
}

// Evidence that some OTHER consumer will deliver and archive this result.
// Two kinds, and the order matters.
//
// 1. Ledger membership — authoritative. A consumer that persists its in-flight
//    set has already told us, as a fact about claiming, that the result is
//    spoken for. It does not depend on any label.
// 2. Source label — a residual net for bridges whose in-flight set is only
//    in-memory, so nothing durable can be consulted for them.
//
// The label list is NOT a closed set and must not be read as one: the gateway
// writes `source: {task.source or PROVIDER}` where PROVIDER comes from
// $REMOTE_TASK_PROVIDER, so an operator can emit a label no list anticipates.
// That is precisely why the ledger check has to come first rather than this
// list being extended each time a new label is observed.
export const NETWORK_CONSUMER_SOURCES = [
	'discord', 'ag2space', 'remote', 'telegram', 'slack', 'whatsapp',
];

/** What a retirement decision may read about a task's origin. */
export interface TaskOrigin {
	source: string | null;
	/** Present in another consumer's durable in-flight ledger. */
	claimedElsewhere?: boolean;
}

export function hasNetworkConsumer(origin: TaskOrigin | null): boolean {
	// Unreadable origin is treated as foreign. Wrongly retiring strands an
	// owner reply; wrongly keeping only accumulates a file, and suppression
	// is universal either way — so the unknown case fails toward keeping.
	if (origin === null) return true;
	if (origin.claimedElsewhere) return true;
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
