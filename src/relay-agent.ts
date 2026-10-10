/**
 * Sutando's relay agent: the voice side's task manager between the voice model and the core, run
 * as the bodhi subagent behind the `work` tool (a Gemini async, NON_BLOCKING tool). Each `work`
 * call waits for its task's result and returns it as the function response, so Gemini decides
 * when to say it and which call it answers. It keeps a table of every task the user asked for by
 * voice (what, when, cancel asked, how its result reached the user, which result answered it) and
 * answers from it; a result whose call is gone is delivered another way and reconciled.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { PersistentSubagentInstance, SubagentConfig } from 'bodhi-realtime-agent';
import { frameTaskResult, framedSystem } from './inject-framing.js';

/** How a voice task's result reached the user. `injected`: handed to the model, turn never confirmed. */
export type Delivery = 'spoken' | 'injected' | 'dm';

/** One row per task the user asked for by voice. Where the task stands is read from the core, not stored here. */
export interface VoiceTaskRecord {
	text?: string;
	submittedAt?: number;
	cancelRequested?: boolean;
	delivery?: Delivery;
	/** Times the relay handed an unheard result over again. */
	replays?: number;
	/** The user was told the core had not picked it up. */
	notPickedNoticed?: boolean;
	/** The core answered this task in another task's result (`[deduped: <id>]`): its outcome is that result. */
	answeredBy?: string;
	/** Last change, for the cap. */
	at: number;
}

const STORE_VERSION = 2;
const STORE_CAP = 200;

/** `state/voice-tasks.json`: the voice task table. One writer, atomic, capped; survives a restart. */
export function createVoiceTaskStore(path: string, now: () => number = Date.now) {
	const read = (): Record<string, VoiceTaskRecord> => {
		try {
			const d = JSON.parse(readFileSync(path, 'utf-8')) as { version?: unknown; tasks?: unknown };
			if (d.version !== STORE_VERSION || !d.tasks || typeof d.tasks !== 'object') return {};
			return d.tasks as Record<string, VoiceTaskRecord>;
		} catch {
			return {};
		}
	};
	const update = (taskId: string, change: (row: VoiceTaskRecord) => VoiceTaskRecord | null) => {
		const tasks = read();
		const next = change(tasks[taskId] ?? { at: 0 });
		if (!next) return;
		tasks[taskId] = { ...next, at: now() };
		const kept = Object.entries(tasks).sort((a, b) => b[1].at - a[1].at).slice(0, STORE_CAP);
		try {
			if (!existsSync(dirname(path))) mkdirSync(dirname(path), { recursive: true });
			const tmp = `${path}.${process.pid}.tmp`;
			writeFileSync(tmp, JSON.stringify({ version: STORE_VERSION, tasks: Object.fromEntries(kept) }));
			renameSync(tmp, path);
		} catch (err) {
			// A row that cannot be written costs a possible repeat, never a lost result.
			console.error(`[RelayAgent] task table write failed: ${err instanceof Error ? err.message : err}`);
		}
	};
	return {
		get(taskId: string): VoiceTaskRecord | undefined {
			return read()[taskId];
		},
		/** Every row, oldest submission first. */
		list(): Array<[string, VoiceTaskRecord]> {
			return Object.entries(read()).sort((a, b) => (a[1].submittedAt ?? a[1].at) - (b[1].submittedAt ?? b[1].at));
		},
		/** A `work` task was submitted. */
		add(taskId: string, text: string): void {
			update(taskId, (row) => ({ ...row, text: text.slice(0, 200), submittedAt: now() }));
		},
		/** The user asked to cancel it. */
		markCancelRequested(taskId: string): void {
			update(taskId, (row) => ({ ...row, cancelRequested: true }));
		},
		set(taskId: string, delivery: Delivery): void {
			// A confirmed delivery is never downgraded by a later, weaker one.
			update(taskId, (row) => (row.delivery === 'spoken' && delivery !== 'spoken' ? null : { ...row, delivery }));
		},
		noteReplay(taskId: string): void {
			update(taskId, (row) => ({ ...row, replays: (row.replays ?? 0) + 1 }));
		},
		noteNotPicked(taskId: string): void {
			update(taskId, (row) => ({ ...row, notPickedNoticed: true }));
		},
		/** The core answered `taskId` in `target`'s result. */
		setAnsweredBy(taskId: string, target: string): void {
			if (taskId === target) return;
			update(taskId, (row) => ({ ...row, answeredBy: target }));
		},
		/** The rows the core answered in `target`'s result. */
		answeredBy(target: string): Array<[string, VoiceTaskRecord]> {
			return Object.entries(read()).filter(([, row]) => row.answeredBy === target);
		},
	};
}

export type VoiceTaskStore = ReturnType<typeof createVoiceTaskStore>;

/** Where the core says a task stands, read fresh each time. */
export type CoreTaskState = 'queued' | 'started' | 'done' | 'cancelled' | 'unknown';

export interface ReconcileInput {
	row: VoiceTaskRecord;
	core: CoreTaskState;
	/** The core wrote a result and the watcher has already handled it (not one still landing). */
	settledResult: boolean;
	/** That result is a skip marker ([deduped: X], [no-send], [REPLIED]): its outcome lives elsewhere. */
	resultIsSkip: boolean;
	/** The result is in the queue right now. */
	inFlight: boolean;
	now: number;
}

export type ReconcileAction = 'none' | 'speak_result' | 'tell_not_picked';

/** Hand an unheard result over at most this many more times. */
export const MAX_REPLAYS = 2;
/** A task the core has not picked up after this long is reported once. */
export const NOT_PICKED_MS = 3 * 60 * 1000;

/**
 * The relay agent's one rule: every voice task ends in an outcome the user heard. Compares what
 * the core did with what the user was told and returns what is still owed.
 */
export function planReconcile(i: ReconcileInput): ReconcileAction {
	if (i.settledResult) {
		if (i.resultIsSkip || i.inFlight) return 'none';
		if (i.row.delivery === 'spoken' || i.row.delivery === 'injected') return 'none';
		return (i.row.replays ?? 0) < MAX_REPLAYS ? 'speak_result' : 'none';
	}
	if (i.core === 'queued' && !i.row.cancelRequested && !i.row.notPickedNoticed
		&& i.row.submittedAt !== undefined && i.now - i.row.submittedAt > NOT_PICKED_MS) return 'tell_not_picked';
	return 'none';
}

export interface ResultItem {
	text: string;
	note?: string;
	taskId?: string;
	/** `text` is already framed for the model (not a task result), e.g. a finished phone call. */
	framed?: boolean;
	/** What the user asked for, so the model can match the result to each request it answers. */
	requests?: string[];
	/** Other voice tasks this result answers (the core deduped them into it): recorded with it. */
	alsoFor?: string[];
}

/** The text handed to the model for one result: the requests it answers, the result, any note. */
export function frameResult(i: ResultItem): string {
	const quote = (r: string) => `"${r.replace(/"/g, "'")}"`;
	const rs = (i.requests ?? []).filter(Boolean);
	const asked = rs.length === 0
		? ''
		: rs.length === 1
			? `${framedSystem(`This answers the user's request: ${quote(rs[0])}.`)}\n\n`
			: `${framedSystem(`This answers ${rs.length} of the user's requests: ${rs.map(quote).join('; ')}. Cover each of them.`)}\n\n`;
	return asked + (i.framed ? i.text : frameTaskResult(i.text)) + (i.note ? `\n\n${framedSystem(i.note)}` : '');
}

/** Submits one `work` call to the core: the tool's own execute, returning its status object. */
export type SubmitWork = (args: Record<string, unknown>) => Promise<Record<string, unknown>>;

export interface RelayAgentDeps {
	submit: SubmitWork;
	store: VoiceTaskStore;
	/** Says a line to the model now (the queue position at submission). */
	notice?: (text: string) => void;
	log?: (msg: string) => void;
}

interface Waiter {
	resolve: (text: string) => void;
	reject: (err: Error) => void;
	/** The session replaced the connection this call was made on: its response would be dropped. */
	stale: boolean;
}

/**
 * The `work` subagent. `invoke` submits the task and, unless the status is the answer (a duplicate,
 * a rejection, the fast path), waits for the task's result and returns it as the function response.
 * `offerResult` hands a landed result to its waiting call; it refuses (the caller delivers another
 * way) when no call waits, or when the call's connection was replaced (`detachAll`).
 */
export class RelayAgent implements PersistentSubagentInstance {
	readonly key = 'relay-agent';
	private readonly waiting = new Map<string, Waiter>();

	constructor(private readonly deps: RelayAgentDeps) {}

	async invoke(_task: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
		const submitted = await this.deps.submit(args);
		const taskId = submitted.taskId;
		if (submitted.status !== 'pending' || typeof taskId !== 'string') return JSON.stringify(submitted);
		// The ordinary "working on it" is the model's own; a queue position or an offline core is said now.
		const unusual = (typeof submitted.queuedAhead === 'number' && submitted.queuedAhead > 0) || submitted.watcherOnline === false;
		if (unusual && typeof submitted.message === 'string') this.deps.notice?.(framedSystem(submitted.message));
		if (signal?.aborted) throw new Error('aborted');
		return new Promise<string>((resolve, reject) => {
			const waiter: Waiter = { resolve, reject, stale: false };
			this.waiting.set(taskId, waiter);
			signal?.addEventListener('abort', () => {
				if (this.waiting.get(taskId) !== waiter) return;
				this.waiting.delete(taskId);
				this.deps.log?.(`[RelayAgent] ${taskId}: work call ended before its result; delivering it another way`);
				reject(new Error('aborted'));
			}, { once: true });
		});
	}

	/** The session opened a fresh connection: responses to calls made before it would be dropped. */
	detachAll(): void {
		for (const [taskId, waiter] of this.waiting) {
			if (waiter.stale) continue;
			waiter.stale = true;
			this.deps.log?.(`[RelayAgent] ${taskId}: its call's connection was replaced; the result will be delivered another way`);
		}
	}

	/** A result landed. True when its call is waiting on a live connection and now returns it. */
	offerResult(item: ResultItem): boolean {
		const taskId = item.taskId;
		if (!taskId) return false;
		const waiter = this.waiting.get(taskId);
		if (!waiter) return false;
		this.waiting.delete(taskId);
		if (waiter.stale) {
			// Its response would be fenced off; end the call quietly and let the caller deliver.
			waiter.resolve(JSON.stringify({ status: 'delivered_separately', taskId }));
			return false;
		}
		for (const id of [taskId, ...(item.alsoFor ?? [])]) this.deps.store.set(id, 'injected');
		waiter.resolve(frameResult(item));
		// The tasks answered in this result wait on calls whose own result never comes: end them.
		for (const id of item.alsoFor ?? []) {
			const other = this.waiting.get(id);
			if (!other) continue;
			this.waiting.delete(id);
			other.resolve(JSON.stringify({ status: 'answered_together', taskId: id, answeredIn: taskId }));
		}
		return true;
	}

	/** Whether a `work` call is waiting on `taskId`. */
	isWaiting(taskId: string): boolean {
		return this.waiting.has(taskId);
	}

	async dispose(): Promise<void> {
		for (const { reject } of this.waiting.values()) reject(new Error('relay agent disposed'));
		this.waiting.clear();
	}
}

/** The bodhi subagent config for the `work` tool: one relay agent per voice session. */
export function relayAgentSubagentConfig(agent: RelayAgent): SubagentConfig {
	return {
		name: 'relay-agent',
		instructions: 'Relays a work task to the core and returns its result.',
		tools: {},
		lifetime: 'persistent_session',
		persistentFactory: async () => agent,
	};
}
