/**
 * The voice side's task manager: one queue every task result goes through before the model
 * speaks it, and a durable record of how each voice task's result reached the user.
 * Results are handed over one batch at a time, at a pause; a batch counts as spoken only
 * once the model finishes a turn after it.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { frameTaskResult, framedSystem } from './inject-framing.js';

/** How a voice task's result reached the user. `injected`: handed to the model, turn never confirmed. */
export type Delivery = 'spoken' | 'injected' | 'dm';

export interface VoiceTaskRecord { delivery: Delivery; at: number }

const STORE_VERSION = 1;
const STORE_CAP = 200;

/** `state/voice-tasks.json`: voice delivery only; where a task stands stays the core's record. */
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
	return {
		get(taskId: string): VoiceTaskRecord | undefined {
			return read()[taskId];
		},
		set(taskId: string, delivery: Delivery): void {
			const tasks = read();
			// A confirmed delivery is never downgraded by a later, weaker one.
			if (tasks[taskId]?.delivery === 'spoken' && delivery !== 'spoken') return;
			tasks[taskId] = { delivery, at: now() };
			const kept = Object.entries(tasks).sort((a, b) => b[1].at - a[1].at).slice(0, STORE_CAP);
			try {
				if (!existsSync(dirname(path))) mkdirSync(dirname(path), { recursive: true });
				const tmp = `${path}.${process.pid}.tmp`;
				writeFileSync(tmp, JSON.stringify({ version: STORE_VERSION, tasks: Object.fromEntries(kept) }));
				renameSync(tmp, path);
			} catch { /* a record that cannot be written costs a possible repeat, never a lost result */ }
		},
	};
}

export type VoiceTaskStore = ReturnType<typeof createVoiceTaskStore>;

/** The user never heard this result, so a repeat of the request should get it spoken. */
export function shouldReplayDeduped(record: VoiceTaskRecord | undefined): boolean {
	return record?.delivery !== 'spoken' && record?.delivery !== 'injected';
}

export interface ResultItem {
	text: string;
	note?: string;
	taskId?: string;
	attempts?: number;
	/** `text` is already framed for the model (not a task result), e.g. a finished phone call. */
	framed?: boolean;
}

/** The text handed to the model for one batch: one result as before; several, each to be covered. */
export function frameBatch(items: ResultItem[]): string {
	const one = (i: ResultItem) => (i.framed ? i.text : frameTaskResult(i.text)) + (i.note ? `\n\n${framedSystem(i.note)}` : '');
	if (items.length === 1) return one(items[0]);
	const head = framedSystem(`${items.length} task results arrived together. Tell the user about every one of them, one sentence each, in the order given; do not skip any.`);
	return [head, ...items.map(one)].join('\n\n');
}

export interface ResultQueueDeps {
	/** The model must stay silent (a meeting): results wait, they are not sent elsewhere. */
	held?: () => boolean;
	/** The session can take a result now (live, client connected). */
	canInject: () => boolean;
	inject: (text: string) => void;
	/** Resolves at a pause in the conversation (bounded). */
	waitForQuiet: () => Promise<void>;
	/** The session could not take the batch: deliver it another way. */
	fallback: (items: ResultItem[]) => void;
	store?: VoiceTaskStore;
	log?: (msg: string) => void;
	/** Results arriving within this window go out as one batch. */
	gatherMs?: number;
	/** Retries while the session cannot take a result, before the fallback. */
	notReadyRetriesMs?: number[];
	/** How long a handed-over batch waits for its turn to finish. */
	turnTimeoutMs?: number;
	/** Times a batch is handed over again after the user cut its answer off. */
	maxAttempts?: number;
	/** How often a held queue checks whether the meeting is over. */
	heldPollMs?: number;
	sleep?: (ms: number) => Promise<void>;
}

type TurnOutcome = 'ended' | 'interrupted' | 'timeout';

export function createResultQueue(deps: ResultQueueDeps) {
	const gatherMs = deps.gatherMs ?? 2_000;
	const retries = deps.notReadyRetriesMs ?? [1_500, 1_500];
	const turnTimeoutMs = deps.turnTimeoutMs ?? 30_000;
	const maxAttempts = deps.maxAttempts ?? 2;
	const heldPollMs = deps.heldPollMs ?? 2_000;
	const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
	const log = deps.log ?? (() => {});
	const queue: ResultItem[] = [];
	let running = false;
	let settleTurn: ((o: TurnOutcome) => void) | null = null;

	const record = (items: ResultItem[], delivery: Delivery) => {
		for (const i of items) if (i.taskId) deps.store?.set(i.taskId, delivery);
	};

	const awaitTurn = () => new Promise<TurnOutcome>((resolve) => {
		const timer = setTimeout(() => { settleTurn = null; resolve('timeout'); }, turnTimeoutMs);
		settleTurn = (o) => { clearTimeout(timer); settleTurn = null; resolve(o); };
	});

	async function drain(): Promise<void> {
		running = true;
		try {
			while (queue.length > 0) {
				if (deps.held?.()) {
					log(`[TaskManager] meeting mode: holding ${queue.length} result(s) until it ends`);
					while (deps.held()) await sleep(heldPollMs);
				}
				await sleep(gatherMs);
				await deps.waitForQuiet();
				if (deps.held?.()) continue;
				let ready = deps.canInject();
				for (let i = 0; !ready && i < retries.length; i++) {
					await sleep(retries[i]);
					ready = deps.canInject();
				}
				const batch = queue.splice(0, queue.length);
				if (!ready) {
					log(`[TaskManager] session cannot take ${batch.length} result(s); falling back`);
					deps.fallback(batch);
					record(batch, 'dm');
					continue;
				}
				const turn = awaitTurn();
				deps.inject(frameBatch(batch));
				log(`[TaskManager] handed over ${batch.length} result(s): ${batch.map((i) => i.taskId ?? '-').join(', ')}`);
				const outcome = await turn;
				if (outcome === 'interrupted') {
					const retry = batch.filter((i) => (i.attempts ?? 1) < maxAttempts);
					if (retry.length > 0) {
						log(`[TaskManager] answer cut off; handing ${retry.length} result(s) over again at the next pause`);
						queue.unshift(...retry.map((i): ResultItem => ({ ...i, attempts: (i.attempts ?? 1) + 1 })));
						record(batch.filter((i) => !retry.includes(i)), 'injected');
						continue;
					}
				}
				record(batch, outcome === 'ended' ? 'spoken' : 'injected');
			}
		} finally {
			running = false;
		}
	}

	return {
		enqueue(item: ResultItem): void {
			queue.push(item);
			if (!running) void drain();
		},
		/** The model finished a turn. */
		onTurnEnd(): void { settleTurn?.('ended'); },
		/** The user cut the model's turn off. */
		onTurnInterrupted(): void { settleTurn?.('interrupted'); },
		get pending(): number { return queue.length; },
	};
}
