// Suppression is universal; retirement authority is scoped to the consumer
// that dispatched the task. One predicate for both narrowed both.

// All three are ONE `skip` kind in parse_markers(); grammar mirrors it.
export const SKIP_MARKER_RE = /^\s*(?:\[(?:no-send|REPLIED)\]|\[deduped:\s*[^\]]+\])/i;

// Pool cores prepend `**[core: N]**` + optional `_(...)_`; parse_markers peels
// it before any marker scan (result_markers.py:135), so this must too.
export const D7_HEADER_RE = /^\*\*\[core:\s*[^\]]+\]\*\*\s*\n(?:_[^\n]*_\s*\n)?\s*/;

/** True iff `result`'s body carries a skip marker, D7 header peeled first. */
export function bodyIsSkipMarked(result: string): boolean {
	return SKIP_MARKER_RE.test(String(result ?? "").replace(D7_HEADER_RE, ""));
}

export function isSkipMarked(file: string, result: string): boolean {
	return file.startsWith('task-') && bodyIsSkipMarked(result);
}

// Evidence that some OTHER consumer will deliver and archive this result.
// Two kinds, and the order matters.
//
// 1. Ledger membership — authoritative. A consumer that persists its in-flight
//    set has already told us, as a fact about claiming, that the result is
//    spoken for. It does not depend on any label.
// 2. Source label — for bridges whose in-flight set is only in-memory, AND
//    for any ledger the reader cannot reach. A miss reads as "no claim", not
//    "cannot tell", so on that path the label is the whole decision.
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
	// Two nulls, opposite polarity. No task file = unknown, so keep (wrongly
	// retiring strands a reply; wrongly keeping costs a file). A readable
	// header with no `source:` is positive evidence of a local writer.
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
