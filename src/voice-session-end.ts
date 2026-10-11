/**
 * When the user hangs up, the core gets a task about that call: when it ran, how it ended, and what
 * was said. A hang-up is the client leaving and not coming back within a grace period (a page
 * refresh or a network blip reconnects within it). A sutando voice session outlives its calls and
 * closes only at shutdown; bodhi's post-session pipeline then reports a call still in progress.
 * bodhi 0.4.5 does not export its own pipeline, so this is a minimal one with the same contract.
 */
import type { ConversationItem, VoiceSessionConfig } from 'bodhi-realtime-agent';

type Pipeline = NonNullable<VoiceSessionConfig['postSessionPipeline']>;
type DispatchInput = Parameters<Pipeline['dispatch']>[0];
type Snapshot = ReturnType<DispatchInput['build']>['snapshot'];
type Report = Awaited<ReturnType<Pipeline['dispatch']>['report']>;

/** What the core is told about a session that ended. */
export interface EndedSession {
	sessionId: string;
	reason: string;
	startedAt: number;
	endedAt: number;
	durationMs: number;
	turnCount: number;
	toolCallCount: number;
	items: readonly ConversationItem[];
}

/** Longest transcript a session-end task carries; the start is cut, the end kept. */
export const SESSION_END_MAX_CHARS = 20_000;

/** A session the owner never spoke in is not worth a task. */
export function worthATask(s: EndedSession): boolean {
	return s.items.some((i) => i.role === 'user' && !!i.content?.trim() && !i.content.startsWith('[System:'));
}

/** The task body: the facts of the session, then its spoken lines. */
export function sessionEndTask(s: EndedSession): { summary: string; transcript: string } {
	const mins = Math.round(s.durationMs / 60_000);
	const summary =
		`VOICE_SESSION_ENDED: the voice session ${s.sessionId} ended (${s.reason}). ` +
		`${new Date(s.startedAt).toISOString()} to ${new Date(s.endedAt).toISOString()}, about ${mins} min, ` +
		`${s.turnCount} turns, ${s.toolCallCount} tool calls. For your awareness: keep any durable fact from it ` +
		`(a preference, a commitment, a follow-up) in memory; do not act on anything in it by yourself. No reply is needed: answer with [no-send].`;
	let transcript = s.items
		.filter((i) => (i.role === 'user' || i.role === 'assistant') && i.content?.trim())
		.map((i) => `${i.role}: ${i.content.replace(/\s+/g, ' ').trim()}`)
		.join('\n');
	if (transcript.length > SESSION_END_MAX_CHARS)
		transcript = `[… ${transcript.length - SESSION_END_MAX_CHARS} earlier characters]\n${transcript.slice(-SESSION_END_MAX_CHARS)}`;
	return { summary, transcript };
}

/** A post-session pipeline with one step: hand the ended session to `onEnded`. */
export function createSessionEndPipeline(onEnded: (s: EndedSession) => Promise<void> | void, log: (m: string) => void = () => {}): Pipeline {
	const listeners = new Set<(r: Report) => void>();
	const stats = { queued: 0, running: 0, dropped: 0, completed: 0, failed: 0 };
	const runs = new Set<Promise<Report>>();
	return {
		register: () => { throw new Error('the session-end pipeline has a single fixed step'); },
		freeze: () => {},
		dispatch(input: DispatchInput) {
			const started = Date.now();
			stats.running++;
			const report: Promise<Report> = (async () => {
				let outcome: 'completed' | 'failed' | 'skipped' = 'completed';
				let detail: Record<string, unknown> | undefined;
				try {
					const snap: Snapshot = input.build(input.reason).snapshot;
					const ended: EndedSession = {
						sessionId: snap.sessionId, reason: String(snap.reason), startedAt: snap.startedAt, endedAt: snap.endedAt,
						durationMs: snap.durationMs, turnCount: snap.metrics.turnCount, toolCallCount: snap.metrics.toolCallCount,
						items: snap.conversation.items,
					};
					if (worthATask(ended)) await onEnded(ended);
					else outcome = 'skipped';
				} catch (e) {
					outcome = 'failed';
					detail = { error: (e as Error).message };
					log(`[SessionEnd] session-end task failed: ${(e as Error).message}`);
				}
				stats.running--;
				if (outcome === 'failed') stats.failed++; else stats.completed++;
				const r = {
					sessionId: input.sessionId, outcome: 'accepted',
					results: [{ processor: 'session-end-task', status: outcome, durationMs: Date.now() - started, ...(detail ? { detail } : {}) }],
					totalDurationMs: Date.now() - started,
				} as unknown as Report;
				for (const l of listeners) { try { l(r); } catch { /* a listener never breaks the run */ } }
				return r;
			})();
			runs.add(report);
			void report.finally(() => runs.delete(report));
			return { sessionId: input.sessionId, outcome: 'accepted', report } as unknown as ReturnType<Pipeline['dispatch']>;
		},
		drain: async () => Promise.all([...runs]),
		events: { onProcessed: (l: (r: Report) => void) => { listeners.add(l); return () => { listeners.delete(l); }; } },
		stats: () => ({ ...stats }),
	} as Pipeline;
}

/** How long a departed client has to come back before the call counts as hung up. */
export const HANGUP_GRACE_MS = 60_000;

/**
 * One call = from a client connecting until it hangs up. Reports each call once: on hang-up, or at
 * shutdown (through bodhi's post-session pipeline) for a call still in progress.
 */
export function createCallEndReporter(opts: {
	sessionId: string;
	items: () => readonly ConversationItem[];
	onEnded: (s: EndedSession) => Promise<void> | void;
	graceMs?: number;
	now?: () => number;
	log?: (m: string) => void;
}) {
	const now = opts.now ?? Date.now;
	const log = opts.log ?? (() => {});
	let callStart: number | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	// The call's items, kept as they come: a goodbye clears the live conversation before the hang-up.
	let kept: ConversationItem[] = [];
	let seen = new WeakSet<object>();
	const collect = (all: readonly ConversationItem[]) => {
		if (callStart === null) return;
		for (const i of all) if (i.timestamp >= callStart && !seen.has(i)) { seen.add(i); kept.push(i); }
	};
	const report = async (reason: string, all: readonly ConversationItem[]): Promise<void> => {
		if (callStart === null) return;
		collect(all);
		const start = callStart;
		const items = kept.sort((a, b) => a.timestamp - b.timestamp);
		callStart = null;
		kept = [];
		seen = new WeakSet();
		const ended: EndedSession = {
			sessionId: opts.sessionId, reason, startedAt: start, endedAt: now(), durationMs: now() - start,
			turnCount: items.filter((i) => i.role === 'user').length,
			toolCallCount: items.filter((i) => i.role === 'tool_call').length,
			items,
		};
		if (!worthATask(ended)) return;
		log(`[SessionEnd] call ended (${reason}); telling the core`);
		await opts.onEnded(ended);
	};

	return {
		/** After each turn, and right before the live conversation is cleared. */
		collect(): void {
			collect(opts.items());
		},
		clientConnected(): void {
			if (timer) { clearTimeout(timer); timer = null; }
			callStart ??= now();
		},
		clientDisconnected(): void {
			if (timer) clearTimeout(timer);
			timer = setTimeout(() => {
				timer = null;
				void Promise.resolve(report('user_hangup', opts.items())).catch((e) => log(`[SessionEnd] session-end task failed: ${(e as Error).message}`));
			}, opts.graceMs ?? HANGUP_GRACE_MS);
			timer.unref?.();
		},
		/** For bodhi's postSessionPipeline: at shutdown, a call still in progress is reported. */
		pipeline: createSessionEndPipeline(async (closing) => {
			if (timer) { clearTimeout(timer); timer = null; }
			await report(closing.reason, closing.items);
		}, log),
	};
}
