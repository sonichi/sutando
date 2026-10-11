// Suppression is universal; retirement authority is scoped to the consumer
// that dispatched the task. One predicate for both narrowed both.

// All three are ONE `skip` kind in parse_markers(); grammar mirrors it.
// `*` not `+`: `[deduped:]` and `[deduped: ]` both parse (result_markers.py:119).
export const SKIP_MARKER_RE = /^\s*(?:\[(?:no-send|REPLIED)\]|\[deduped:\s*[^\]]*\])/i;

// Pool cores prepend `**[core: N]**` + optional `_(...)_`; parse_markers peels
// it before any marker scan (result_markers.py:135), so this must too.
export const D7_HEADER_RE = /^\*\*\[core:\s*[^\]]+\]\*\*\s*\n(?:_[^\n]*_\s*\n)?\s*/;

// Lines voice never speaks: a standalone `[dm-only]` anywhere, and a bare
// `[thread]` in the leading marker lines (result_markers.py _THREAD_ASK_RE).
const THREAD_ASK_LINE_RE = /^[ \t]*\[thread\][ \t]*\r?$/i;
const DM_ONLY_LINE_RE = /^[ \t]*\[dm-only\][ \t]*\r?$/i;
const LEADING_MARKER_LINE_RE = /^[ \t]*\[(?:channel:[^\]\n]*|thread:[^\]\n]*|reply:[ \t]*\d{17,20})\][ \t]*\r?$/i;

/** The text voice/log callbacks may show: control-only lines removed, prose untouched.
 *  Presentation only: skip and ownership decisions read the raw body. */
export function stripVoiceControlLines(text: string): string {
	const raw = String(text ?? '');
	const header = D7_HEADER_RE.exec(raw)?.[0] ?? '';
	const lines = raw.slice(header.length).split('\n');
	let i = 0;
	const out: string[] = [];
	// Lines are judged before the dm-only strip, so "[dm-only] [thread]" stays prose.
	for (; i < lines.length; i++) {
		const line = lines[i];
		if (THREAD_ASK_LINE_RE.test(line)) continue;
		if (line.trim() !== '' && !LEADING_MARKER_LINE_RE.test(line) && !DM_ONLY_LINE_RE.test(line)) break;
		out.push(line);
	}
	return (header + out.concat(lines.slice(i)).join('\n')).replace(/^[ \t]*\[dm-only\][ \t]*\r?\n?/gim, '');
}

// parse_markers' leading-marker loop, mirrored for the skip decision only
// (skip_after_channel=True: voice delivers owner results, which no guard holds for review).
const LEAD_REDIRECT_RE = /^\s*\[channel:\s*[^\]]*\]\s*\n?/;
const LEAD_THREAD_RE = /^\s*\[thread:\s*[^\]]*\]\s*\n?/i;
const LEAD_THREAD_ASK_RE = /^\s*\[thread\][ \t]*(?:\r?\n|$)/i;
const LEAD_REPLY_RE = /^\s*\[reply:\s*\d{17,20}\]\s*\n?/;
const DM_ONLY_STRIP_RE = /^[ \t]*\[dm-only\][ \t]*\r?\n?/gim;

/** The body after the leading markers, as parse_markers reaches it (D7 already peeled). */
function afterLeadingMarkers(body: string): string {
	const glued = new Set<number>();
	let lead = body;
	if (/\[dm-only\]/i.test(body)) {
		let out = '';
		let last = 0;
		for (const m of body.matchAll(DM_ONLY_STRIP_RE)) {
			out += body.slice(last, m.index);
			if (!m[0].endsWith('\n') && m.index! + m[0].length < body.length) glued.add(out.length);
			last = m.index! + m[0].length;
		}
		lead = out + body.slice(last);
	}
	let rest = lead;
	for (;;) {
		const channel = LEAD_REDIRECT_RE.exec(rest);
		if (channel) { rest = rest.slice(channel[0].length); continue; }
		const fixed = [LEAD_THREAD_RE, LEAD_REPLY_RE].map(re => re.exec(rest)).find(Boolean);
		if (fixed) { rest = rest.slice(fixed[0].length); continue; }
		const ask = LEAD_THREAD_ASK_RE.exec(rest);
		const at = lead.length - rest.length + (ask ? ask[0].indexOf('[') : 0);
		const start = lead.slice(0, at).replace(/[ \t]+$/, '').length;
		if (ask && (start === 0 || lead[start - 1] === '\n') && !glued.has(at) && !glued.has(start)) {
			rest = rest.slice(ask[0].length);
			continue;
		}
		return rest;
	}
}

/** True iff `result`'s body is a skip in parse_markers(..., skip_after_channel=True): a skip
 *  marker first, or directly after the leading markers. D7 header peeled first. */
export function bodyIsSkipMarked(result: string): boolean {
	const body = String(result ?? "").replace(D7_HEADER_RE, "");
	return SKIP_MARKER_RE.test(body) || SKIP_MARKER_RE.test(afterLeadingMarkers(body));
}

/** The task whose result a `[deduped: <task-id>]` result points to, D7 header peeled first; null otherwise. */
export function dedupTarget(result: string): string | null {
	const m = /^\s*\[deduped:\s*(task-[A-Za-z0-9._-]+)\s*\]/i.exec(String(result ?? "").replace(D7_HEADER_RE, ""));
	return m ? m[1] : null;
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
