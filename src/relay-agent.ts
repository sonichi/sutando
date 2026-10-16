/**
 * Sutando's relay agent: the voice side's task manager between the voice model and the core, run
 * as the bodhi subagent behind the `work` tool. The model hears the tool's pending message at once;
 * each `work` call waits for its task's result and returns it, and bodhi hands it to the model as a
 * system notification when the model is idle. It keeps a table of every task the user asked for by
 * voice (what, when, cancel asked, how its result reached the user, which result answered it) and
 * answers from it; a result whose call is gone is delivered another way and reconciled.
 */

import type { PersistentSubagentInstance, SubagentConfig } from 'bodhi-realtime-agent';
import { frameTaskResult, framedSystem } from './inject-framing.js';
import type { OpenTask, TurnClassifier, WorkRequestCheck } from './work-request-check.js';

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
	/** The user corrected this request with a later task: its own result is not said. */
	supersededBy?: string;
	/** The core ended it with nothing for the user to hear (a skip-marked result that answers no other task). */
	retired?: boolean;
	/** Last change, for the cap. */
	at: number;
}

const STORE_CAP = 200;

/**
 * The voice task table, in memory: one row per `work` task the user asked for by voice, with where the
 * core stands and whether the user heard the answer. The voice agent owns it, like bodhi's
 * ConversationContext; copies go out through `onChange` listeners, the way bodhi's history stores
 * write the conversation (the status page reads one). Capped at the newest STORE_CAP rows.
 */
export function createVoiceTaskStore(now: () => number = Date.now) {
	const tasks = new Map<string, VoiceTaskRecord>();
	// Called after every change, like bodhi's history stores after a turn: they copy the table out
	// (asynchronously, on their own schedule); the table never waits for them.
	const listeners = new Set<() => void>();
	const update = (taskId: string, change: (row: VoiceTaskRecord) => VoiceTaskRecord | null) => {
		const next = change(tasks.get(taskId) ?? { at: 0 });
		if (!next) return;
		tasks.set(taskId, { ...next, at: now() });
		if (tasks.size > STORE_CAP) {
			for (const [id] of [...tasks.entries()].sort((a, b) => a[1].at - b[1].at).slice(0, tasks.size - STORE_CAP)) tasks.delete(id);
		}
		for (const l of listeners) {
			try { l(); } catch (err) { console.error(`[RelayAgent] task table listener failed: ${err instanceof Error ? err.message : err}`); }
		}
	};
	return {
		/** Called after each change to the table; returns the unsubscribe. */
		onChange(listener: () => void): () => void {
			listeners.add(listener);
			return () => listeners.delete(listener);
		},
		get(taskId: string): VoiceTaskRecord | undefined {
			return tasks.get(taskId);
		},
		/** Every row, oldest submission first. */
		list(): Array<[string, VoiceTaskRecord]> {
			return [...tasks.entries()].sort((a, b) => (a[1].submittedAt ?? a[1].at) - (b[1].submittedAt ?? b[1].at));
		},
		/** A `work` task was submitted (`submittedAt` for a row rebuilt from an older submission). */
		add(taskId: string, text: string, submittedAt: number = now()): void {
			update(taskId, (row) => ({ ...row, text: text.slice(0, 200), submittedAt }));
		},
		/** The user asked to cancel it. */
		markCancelRequested(taskId: string): void {
			update(taskId, (row) => ({ ...row, cancelRequested: true }));
		},
		set(taskId: string, delivery: Delivery): void {
			// A confirmed delivery is never downgraded by a later, weaker one.
			const deliver = (row: VoiceTaskRecord) => (row.delivery === 'spoken' && delivery !== 'spoken' ? null : { ...row, delivery });
			update(taskId, deliver);
			// The tasks answered in this one's result reached the user with it, whichever way it went.
			for (const [id, row] of [...tasks.entries()]) if (row.answeredBy === taskId) update(id, deliver);
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
			// The answering result may already have been handed over: this task went with it.
			const reached = tasks.get(target)?.delivery;
			update(taskId, (row) => ({ ...row, answeredBy: target, ...(reached && row.delivery !== 'spoken' ? { delivery: reached } : {}) }));
		},
		/** The user corrected `taskId` with `by`. */
		markRetired(taskId: string): void {
			update(taskId, (row) => ({ ...row, retired: true }));
		},
		setSupersededBy(taskId: string, by: string): void {
			if (taskId === by) return;
			update(taskId, (row) => ({ ...row, supersededBy: by }));
		},
		/** The rows the core answered in `target`'s result. */
		answeredBy(target: string): Array<[string, VoiceTaskRecord]> {
			return [...tasks.entries()].filter(([, row]) => row.answeredBy === target);
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
	if (i.row.supersededBy) return 'none';
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
	/** A status notice about this task (not its result): not said once it is no longer true. */
	about?: string;
}

// Results can arrive minutes later and several in a row: the user hears which request each one answers.
export const SAY_WHICH_ONE = 'Start by saying in a few words, in the user\'s language, which request this answers (e.g. "About the PR 5200 comment: …"), then the answer.';
export const SAY_WHICH_EACH = 'Name each request, in a few words in the user\'s language, before its answer.';

/** The text handed to the model for one result: the requests it answers, the result, any note. */
export function frameResult(i: ResultItem): string {
	const quote = (r: string) => `"${r.replace(/"/g, "'")}"`;
	const rs = (i.requests ?? []).filter(Boolean);
	const asked = rs.length === 0
		? ''
		: rs.length === 1
			? `${framedSystem(`This answers the user's request: ${quote(rs[0])}. ${SAY_WHICH_ONE}`)}\n\n`
			: `${framedSystem(`This answers ${rs.length} of the user's requests: ${rs.map(quote).join('; ')}. Cover each of them. ${SAY_WHICH_EACH}`)}\n\n`;
	return asked + (i.framed ? i.text : frameTaskResult(i.text)) + (i.note ? `\n\n${framedSystem(i.note)}` : '');
}

/** A correction reaches back this far for the request it replaces. */
export const SUPERSEDE_WINDOW_MS = 60 * 60_000;

/** A result the table says needs no saying: its task was corrected, or every task it answers was heard. */
export function resultIsMoot(store: VoiceTaskStore, ids: string[]): boolean {
	if (!ids.length) return false;
	if (store.get(ids[0])?.supersededBy) return true;
	return ids.every((id) => store.get(id)?.delivery === 'spoken');
}

/** What identifies a request beyond doubt: numbers of three or more digits, and quoted names (a correction names one). */
export function requestMarks(request: string): string[] {
	const numbers = request.match(/\d{3,}/g) ?? [];
	const quoted = [...request.matchAll(/["“「]([^"”」]{2,})["”」]/g)].map((m) => m[1].toLowerCase());
	return [...new Set([...numbers, ...quoted])];
}

/** The part of a table row that the supersede rule reads. */
export type SupersedeRow = Pick<VoiceTaskRecord, 'text' | 'submittedAt' | 'delivery' | 'supersededBy'>;

export interface SupersedeInput {
	/** The request that corrects. */
	taskId: string;
	/** What it corrects, trimmed: a task id, or the misheard number or quoted name. */
	replaces: string;
	/** Oldest submission first, as `VoiceTaskStore.list()` returns them: the last match is the most recent. */
	rows: ReadonlyArray<readonly [string, SupersedeRow]>;
	now: number;
	/** What a task id looks like in `rows`, anchored; the default is the voice task table's `task-<digits>`. */
	idPattern?: RegExp;
}

export type SupersedePlan =
	| { action: 'supersede'; id: string }
	/** `names_nothing`: `replaces` is no id and carries no mark. `no_match`: no unheard request fits it. */
	| { action: 'none'; why: 'names_nothing' | 'no_match' };

const TASK_ID = /^task-\d+$/;

/**
 * The most recent unheard request from the last hour that `replaces` names, by id or by every mark
 * it carries. Only a whole mark counts: a bare word like "pr" would match every unheard request.
 */
export function planSupersede(i: SupersedeInput): SupersedePlan {
	const since = i.now - SUPERSEDE_WINDOW_MS;
	// A copy without g/y: `.test` on the caller's own pattern would move its lastIndex.
	const form = i.idPattern ?? TASK_ID;
	const byId = new RegExp(form.source, form.flags.replace(/[gy]/g, '')).test(i.replaces);
	const marks = byId ? [] : requestMarks(i.replaces);
	if (!byId && marks.length === 0) return { action: 'none', why: 'names_nothing' };
	const target = i.rows
		.filter(([id, row]) => id !== i.taskId && row.delivery !== 'spoken' && !row.supersededBy && (row.submittedAt ?? 0) >= since)
		.filter(([id, row]) => byId ? id === i.replaces : marks.every((m) => requestMarks(row.text ?? '').includes(m)))
		.pop();
	return target ? { action: 'supersede', id: target[0] } : { action: 'none', why: 'no_match' };
}

/** How long a handed-over result waits for the model's turn before the next one goes anyway. */
export const HANDOFF_CONFIRM_MS = 60_000;

/** How long a result waits for the model to answer what the user just said (noise can end a user turn no reply follows). */
export const USER_REPLY_WAIT_MS = 8_000;

/** How long the user stays quiet before a `work` call is submitted: a pause mid-sentence is shorter. */
export const QUIET_BEFORE_SUBMIT_MS = 1_500;

/** A `work` call is never held longer than this, whatever the speech events say. */
export const MAX_SUBMIT_HOLD_MS = 30_000;

/**
 * Whether the user has finished their turn. The model can end the user's turn mid-sentence (a short
 * pause) and call `work` with half a request; the relay agent waits on this before it submits. The
 * turn is over once the user has been quiet a moment, or once the model has spoken to them: what the
 * user says after the model's reply is a new request, never the rest of this one.
 */
/** A speech event's origin. Gemini's provider events mark a start on every transcription chunk and never an end. */
export interface SpeechEvent { source?: string }

/** Only the client's own microphone detection says when the user starts and stops: a provider start has no end. */
export const fromClient = (e?: SpeechEvent): boolean => e?.source !== 'provider';

export interface SpeechGate {
	onUserSpeechStarted(e?: SpeechEvent): void;
	onUserSpeechEnded(e?: SpeechEvent): void;
	/** The model started speaking to the user. */
	onModelAudio(): void;
	/** Resolves once the user's turn is over, or the hold limit or `signal` ends the wait. */
	waitTurnOver(signal?: AbortSignal): Promise<void>;
}

export function createSpeechGate(opts: { quietMs?: number; maxHoldMs?: number; now?: () => number } = {}): SpeechGate {
	const now = opts.now ?? Date.now;
	const quietMs = opts.quietMs ?? QUIET_BEFORE_SUBMIT_MS;
	const maxHoldMs = opts.maxHoldMs ?? MAX_SUBMIT_HOLD_MS;
	let speaking = false;
	let startedAt = 0;
	let endedAt: number | null = null;
	let modelAudioAt: number | null = null;
	// Every wait looks again on each event, so the model's first word releases a call at once.
	const waits = new Set<() => void>();
	const wake = () => { for (const look of [...waits]) look(); };
	return {
		onUserSpeechStarted(e) { if (!fromClient(e)) return; speaking = true; startedAt = now(); wake(); },
		onUserSpeechEnded(e) { if (!fromClient(e)) return; speaking = false; endedAt = now(); wake(); },
		onModelAudio() { modelAudioAt = now(); wake(); },
		waitTurnOver(signal) {
			const since = now();
			return new Promise<void>((resolve) => {
				let timer: ReturnType<typeof setTimeout> | null = null;
				const done = () => { waits.delete(look); if (timer) clearTimeout(timer); resolve(); };
				function look() {
					const t = now();
					if (signal?.aborted || t - since >= maxHoldMs) return done();
					// The model answered: the user's turn is over, whatever the microphone picks up now.
					if (modelAudioAt !== null && modelAudioAt >= since) return done();
					// A speech start with no end (a sound too short to be speech) does not hold the call for good.
					const stillSpeaking = speaking && t - startedAt < maxHoldMs;
					const quietFrom = stillSpeaking ? null : (endedAt ?? 0) + quietMs;
					if (quietFrom !== null && t >= quietFrom) return done();
					if (timer) clearTimeout(timer);
					timer = setTimeout(look, quietFrom === null ? Math.max(10, since + maxHoldMs - t) : Math.max(10, quietFrom - t));
				}
				waits.add(look);
				signal?.addEventListener('abort', look, { once: true });
				look();
			});
		},
	};
}

/** The request a cut-off call sent, joined to the one the user went on to say. */
export function joinCutOffRequest(earlier: string, later: string): string {
	return `${later} (the user also said, just before, cut off mid-sentence: "${earlier.replace(/"/g, "'")}"; if that asks for something else, do it too)`;
}

/** One result on its way to the model. */
export interface Handoff {
	/** The tasks it answers: recorded spoken once the model has said it. */
	ids: string[];
	label: string;
	/** Hands it to the model now (its waiting work call or a system notice). False when it cannot. */
	hand(): boolean;
	/** It could not be handed: deliver it another way. */
	fallback(): void;
	/** The table says it no longer needs saying (checked when its turn comes). */
	moot?(): boolean;
}

/**
 * Hands results to the model one at a time and confirms each from the model's next turn: a turn
 * the model began saying records it spoken, also when it was cut off: said once is said (2026-10-12,
 * a 213 ms sound after a finished sentence counted as a cut-off and the answer was read again). Two results landing at
 * once would otherwise reach the model together, and it may answer only one. A result also waits
 * while the user speaks and until the model has answered them, so it never takes the user's turn.
 * Only a turn the model spoke in counts: a turn that was only a tool call answered no one.
 */
export function createHandoffPacer(deps: {
	markSpoken: (ids: string[]) => void;
	confirmMs?: number;
	userReplyWaitMs?: number;
	now?: () => number;
	log?: (msg: string) => void;
}) {
	const now = deps.now ?? Date.now;
	let userSpeaking = false;
	let userStartedAt = 0;
	let userEndedAt: number | null = null;
	let userTimer: ReturnType<typeof setTimeout> | null = null;
	let spoke = false;
	// A speech start with no end (a dropped event) must not hold results for good.
	const speakingHold = () => userSpeaking && now() - userStartedAt < (deps.confirmMs ?? HANDOFF_CONFIRM_MS);
	const userTurnOpen = () =>
		speakingHold() || (userEndedAt !== null && now() - userEndedAt < (deps.userReplyWaitMs ?? USER_REPLY_WAIT_MS));
	const queue: Handoff[] = [];
	let current: { h: Handoff; started: boolean; timer: ReturnType<typeof setTimeout> } | null = null;
	const busyWith = (ids: string[]) =>
		[...(current ? [current.h] : []), ...queue].some((h) => h.ids.some((id) => ids.includes(id)));
	const release = () => {
		if (current) clearTimeout(current.timer);
		current = null;
		pump();
	};
	const arm = () => {
		const timer = setTimeout(() => {
			deps.log?.(`[RelayAgent] ${current?.h.label}: no turn after the hand-off; moving on`);
			release();
		}, deps.confirmMs ?? HANDOFF_CONFIRM_MS);
		timer.unref?.();
		return timer;
	};
	function pump(): void {
		if (!current && queue.length && userTurnOpen()) {
			// Look again when the wait runs out; every user turn that ends starts it over.
			const until = speakingHold()
				? userStartedAt + (deps.confirmMs ?? HANDOFF_CONFIRM_MS)
				: (userEndedAt ?? now()) + (deps.userReplyWaitMs ?? USER_REPLY_WAIT_MS);
			if (userTimer) clearTimeout(userTimer);
			userTimer = setTimeout(() => { userTimer = null; pump(); }, Math.max(0, until - now()));
			userTimer.unref?.();
			return;
		}
		while (!current && queue.length) {
			const h = queue.shift()!;
			if (h.moot?.()) { deps.log?.(`[RelayAgent] ${h.label}: already heard or corrected; not said`); continue; }
			if (!h.hand()) { h.fallback(); continue; }
			current = { h, started: false, timer: arm() };
		}
	}
	return {
		/** Queue a result; one already queued or on its way for the same task is not queued twice. */
		submit(h: Handoff): void {
			if (h.ids.length && busyWith(h.ids)) return;
			queue.push(h);
			pump();
		},
		onUserSpeechStarted(e?: SpeechEvent): void {
			if (!fromClient(e)) return;
			userSpeaking = true;
			userStartedAt = now();
		},
		onUserSpeechEnded(e?: SpeechEvent): void {
			if (!fromClient(e)) return;
			userSpeaking = false;
			userEndedAt = now();
			if (!current) pump();
		},
		onTurnStart(): void {
			spoke = false;
			if (current) current.started = true;
		},
		/** The model's audio began in this turn. */
		onModelAudio(): void {
			spoke = true;
		},
		onTurnEnd(interrupted: boolean): void {
			const spokeInTurn = spoke;
			spoke = false;
			// A silent turn (a tool call only) answered nothing and said nothing.
			if (!spokeInTurn) return;
			// A finished, spoken model turn answered whatever the user had said.
			if (!interrupted && !userSpeaking) {
				userEndedAt = null;
				if (userTimer) { clearTimeout(userTimer); userTimer = null; }
				if (!current) pump();
			}
			if (!current?.started) return;
			// The model began saying it: said once is said. A cut-off is not a reason to say it again (a
			// short sound after a finished sentence counts as a cut-off), and the table reads it as heard.
			if (interrupted) deps.log?.(`[RelayAgent] ${current.h.label}: answer cut off; not said again`);
			deps.markSpoken(current.h.ids);
			release();
		},
		/** Whether a result for this task is queued or on its way to the model. */
		has(taskId: string): boolean {
			return busyWith([taskId]);
		},
		get pending(): number {
			return queue.length + (current ? 1 : 0);
		},
	};
}

/** The last `n` spoken lines of the live conversation, oldest first, as "user: …" / "assistant: …". System lines are left out. */
export function recentConversationLines(items: ReadonlyArray<{ role?: string; content?: string }>, n: number): string {
	return items
		.filter((i) => (i.role === 'user' || i.role === 'assistant') && i.content?.trim() && !i.content.startsWith('[System:'))
		.slice(-n)
		.map((i) => `${i.role}: ${i.content!.replace(/\s+/g, ' ').trim()}`)
		.join('\n');
}

/** A question waiting for the model to stop speaking is put anyway after this long. */
export const ASK_WAIT_MAX_MS = 15_000;

/**
 * Puts the relay's question to the user as live input as soon as the model is not speaking: at once when it
 * is quiet, else when its turn ends (or after ASK_WAIT_MAX_MS). Only the newest question waits.
 */
export function createAskDelivery(deps: { inject: (text: string) => void; maxWaitMs?: number; log?: (m: string) => void }) {
	let speaking = false;
	let waiting: { text: string; timer: ReturnType<typeof setTimeout> } | null = null;
	const flush = () => {
		if (!waiting) return;
		const { text, timer } = waiting;
		clearTimeout(timer);
		waiting = null;
		deps.inject(text);
	};
	return {
		ask(text: string): void {
			if (!speaking) { deps.inject(text); return; }
			if (waiting) clearTimeout(waiting.timer);
			const timer = setTimeout(() => { deps.log?.('[RelayAgent] the model is still speaking; asking anyway'); flush(); }, deps.maxWaitMs ?? ASK_WAIT_MAX_MS);
			timer.unref?.();
			waiting = { text, timer };
		},
		onModelAudio(): void { speaking = true; },
		onTurnEnd(): void { speaking = false; flush(); },
	};
}

/** Submits one `work` call to the core: the tool's own execute, returning its status object. `origin` is what `captureOrigin` took when the call began. */
export type SubmitWork<O = unknown> = (args: Record<string, unknown>, origin?: O) => Promise<Record<string, unknown>>;

export interface RelayAgentDeps<O = unknown> {
	submit(args: Record<string, unknown>, origin?: O): ReturnType<SubmitWork<O>>;
	/** Takes where a request was asked from (room, words around it) the moment it is made, before any wait. */
	captureOrigin?: () => O;
	store: VoiceTaskStore;
	/** Says a line to the model now (the queue position at submission). */
	notice?: (text: string) => void;
	/** Holds each call until the user has finished speaking. Without it, calls are submitted at once. */
	speech?: SpeechGate;
	/** Decides, before a request is submitted, whether the user must first be asked for a missing piece. */
	check?: WorkRequestCheck;
	/** The recent conversation the check reads. */
	recentConversation?: () => string;
	/** Looks at every finished user turn, whatever tool the voice model chose: is it a request for the core? */
	classify?: TurnClassifier;
	/** Has the voice model put a question to the user as soon as it stops speaking (falls back to `notice`). */
	ask?: (text: string) => void;
	log?: (msg: string) => void;
}

/** After the user was asked about a request, the next call within this window is sent as it is. */
export const ONE_ASK_WINDOW_MS = 2 * 60_000;

/** A request sent this long ago and still unanswered is compared with new ones (a repeat is not sent twice). */
export const OPEN_TASK_WINDOW_MS = 30 * 60_000;

interface Waiter {
	resolve: (text: string) => void;
	reject: (err: Error) => void;
}

/** How a waiting call ends without its result. bodhi reads a rejection out as a failure, so none rejects. */
const ENDED: Record<'cancelled' | 'continuing' | 'delivered_separately' | 'continued_in_next' | 'not_sent', string> = {
	not_sent: 'This call was cancelled before the request was sent; nothing was submitted.',
	continued_in_next: 'The user was not finished: this request was sent together with what they said next. Say nothing about it.',
	cancelled: 'The user cancelled this task and was already told. Do not mention it again.',
	continuing: 'The task is still running; its result will come in a separate message.',
	delivered_separately: 'This task\'s result is delivered to the user separately. Say nothing about it now.',
};

/**
 * The `work` subagent. `invoke` submits the task and, unless the status is the answer (a duplicate,
 * a rejection, the fast path), waits for the task's result and returns it. `offerResult` hands a
 * landed result to its waiting call; it refuses (the caller delivers another way) when no call waits.
 */
export class RelayAgent<O = unknown> implements PersistentSubagentInstance {
	readonly key = 'relay-agent';
	private readonly waiting = new Map<string, Waiter>();
	/** The call waiting for the user to finish speaking, not yet submitted. */
	private holding: { args: Record<string, unknown>; origin: O | undefined; joined: boolean } | null = null;

	/** When the voice model last took a turn itself (`work`, or a tool that fetches the answer): the turn check does not send it again. */
	private lastTakenAt = 0;
	/** When the work check last had the user asked: the next call's answer is sent as it is. */
	private askedAt = 0;
	/** User turns waiting for the turn check (several when the user went on speaking). */
	private turns: Array<{ text: string; at: number; origin: O | undefined }> = [];
	private turnsRunning = false;
	/** When the last turn the turn check took ended: a `work` call after it belongs to the next turn. */
	private lastTurnAt = 0;

	constructor(private readonly deps: RelayAgentDeps<O>) {}

	/** Tasks sent and not answered yet, newest last: what a new request is compared with. */
	openTasks(now = Date.now()): OpenTask[] {
		return this.deps.store.list()
			.filter(([, r]) => !!r.text && !r.delivery && !r.cancelRequested && !r.retired && !r.supersededBy && !r.answeredBy && (r.submittedAt ?? r.at) > now - OPEN_TASK_WINDOW_MS)
			.map(([id, r]) => ({ id, text: r.text as string }));
	}

	/**
	 * The user finished a turn. The turn check decides whether it asks the core for something, whichever
	 * tool the voice model chose (2026-10-12: "帮我查一下 5334 的状态" went to get_task_status and was lost).
	 * A turn the voice model already sent with `work` is left to that call.
	 */
	onUserTurn(text: string, at = Date.now()): void {
		const t = text.trim();
		if (!this.deps.classify || !t || t.startsWith('[System:')) return;
		this.turns.push({ text: t, at, origin: this.deps.captureOrigin?.() });
		if (!this.turnsRunning) void this.drainTurns();
	}

	private async drainTurns(): Promise<void> {
		this.turnsRunning = true;
		try {
			while (this.turns.length) {
				// One request may span several turns: wait until the user is done, then take them together.
				if (this.deps.speech) await this.deps.speech.waitTurnOver();
				const taken = this.turns.splice(0);
				const since = this.lastTurnAt;
				this.lastTurnAt = taken[taken.length - 1].at;
				if (this.lastTakenAt > since) continue; // the voice model took it itself
				const openTasks = this.openTasks();
				const verdict = await this.deps.classify!({
					utterance: taken.map((x) => x.text).join(' '),
					recentConversation: this.deps.recentConversation?.() ?? '',
					openTasks,
				}).catch(() => ({ action: 'none' as const }));
				if (this.lastTakenAt > since) continue; // the voice model's own call came in while the check ran
				await this.actOnTurn(verdict, taken[0].origin);
			}
		} catch (err) {
			this.deps.log?.(`[RelayAgent] turn check failed: ${err instanceof Error ? err.message : String(err)}`);
		} finally {
			this.turnsRunning = false;
		}
	}

	private async actOnTurn(verdict: Awaited<ReturnType<TurnClassifier>>, origin: O | undefined): Promise<void> {
		if (verdict.action === 'none') return;
		if (verdict.action === 'duplicate') {
			this.deps.log?.(`[RelayAgent] the user asked again for ${verdict.of}; not sent twice`);
			return;
		}
		if (verdict.action === 'ask') {
			this.deps.log?.(`[RelayAgent] turn not sent, asking the user: ${verdict.question}`);
			const line = framedSystem(`What the user just asked cannot be sent to the core yet. Ask them now, in their language, exactly: "${verdict.question.replace(/"/g, "'")}"`);
			// Not the notification queue: it can sit behind the model's own answer and the next turn (2026-10-13 00:41:
			// decided at :16, said at :35, after an unrelated noise turn).
			(this.deps.ask ?? this.deps.notice)?.(line);
			return;
		}
		const submitted = await this.deps.submit({ task: verdict.task }, origin);
		const taskId = submitted.taskId;
		if (submitted.status !== 'pending' || typeof taskId !== 'string') {
			this.deps.log?.(`[RelayAgent] turn check: the request was not taken (${String(submitted.status)})`);
			return;
		}
		this.deps.log?.(`[RelayAgent] ${taskId}: sent by the turn check (the voice model had not called work): ${verdict.task}`);
		// Its result comes like any other with no call waiting: handed to the model when it can speak.
		this.deps.notice?.(framedSystem(`This request was sent to the core for the user: "${verdict.task.replace(/"/g, "'")}". If you have not already, tell the user in one short sentence that it is being looked into. Do not call work for it.`));
	}

	/**
	 * The voice model called a tool that fetches the answer itself (read_room): the turn is answered, not
	 * sent to the core as well (2026-10-13 17:08: a room question was read and also sent as a task). A call
	 * that fails tells the voice model so, and it can call work.
	 */
	noteAnsweringTool(name: string): void {
		this.lastTakenAt = Date.now();
		this.deps.log?.(`[RelayAgent] ${name} answers this turn; the turn check sends nothing`);
	}

	async invoke(_task: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
		this.lastTakenAt = Date.now();
		// Taken before the waits below: a room switch during them must not re-address the request.
		let origin = this.deps.captureOrigin?.();
		if (this.deps.speech) {
			// A call made while an earlier one still waits for the user's turn to end: the model has not
			// answered in between, the user went on speaking, so the two are one request. The earlier
			// call ends; this one carries both.
			const earlier = this.holding;
			if (earlier) {
				earlier.joined = true;
				origin = earlier.origin; // the request began with the earlier call
				args = {
					...earlier.args,
					...args,
					task: joinCutOffRequest(String(earlier.args.task ?? ''), String(args.task ?? '')),
				};
				this.deps.log?.('[RelayAgent] the user went on speaking: the earlier request is sent with this one');
			}
			const mine = { args, origin, joined: false };
			this.holding = mine;
			await this.deps.speech.waitTurnOver(signal);
			if (mine.joined) return framedSystem(ENDED.continued_in_next);
			if (this.holding === mine) this.holding = null;
		}
		// bodhi cancelled the call (tool cancellation, the session closing) while it waited for the user or the check.
		if (signal?.aborted) return framedSystem(ENDED.not_sent);
		if (this.deps.check) {
			// A request with a blank in it (which PR? which file?) is not submitted: the user is asked.
			const verdict = await this.deps.check({ task: String(args.task ?? ''), recentConversation: this.deps.recentConversation?.() ?? '', openTasks: this.openTasks() }, signal)
				.catch(() => ({ action: 'submit' as const }));
			if (signal?.aborted) return framedSystem(ENDED.not_sent);
			// One question per request: the answer is sent as it is, even if the check still finds it vague
			// ("the latest one") - the user is not interrogated (review of #5348).
			if (verdict.action === 'ask' && Date.now() - this.askedAt < ONE_ASK_WINDOW_MS) {
				this.deps.log?.(`[RelayAgent] the user was already asked about this; sending it as it is`);
			} else if (verdict.action === 'ask') {
				this.askedAt = Date.now();
				this.deps.log?.(`[RelayAgent] not submitted, asking the user: ${verdict.question}`);
				return framedSystem(`Nothing was submitted: the request does not say what it is about. Ask the user now, in their language, exactly: "${verdict.question.replace(/"/g, "'")}" Then, with their answer, call work again.`);
			}
			if (verdict.action === 'duplicate') {
				// The same request is already with the core (2026-10-12: PR 5333 was sent twice). This call
				// waits for that task's result instead, unless another call already does.
				this.deps.log?.(`[RelayAgent] ${verdict.of}: asked again; not sent twice`);
				if (this.waiting.has(verdict.of)) return framedSystem('This request was already sent to the core and its result will be told when it comes. Tell the user so in one short sentence.');
				return new Promise<string>((resolve, reject) => {
					this.waiting.set(verdict.of, { resolve, reject });
					signal?.addEventListener('abort', () => { this.endCall(verdict.of, 'continuing'); }, { once: true });
				});
			}
		}
		this.askedAt = 0; // this request goes; the next vague one may be asked about again
		const submitted = await this.deps.submit(args, origin);
		const taskId = submitted.taskId;
		if (submitted.status !== 'pending' || typeof taskId !== 'string') return JSON.stringify(submitted);
		const replaces = typeof args.replaces === 'string' ? args.replaces.trim() : '';
		if (replaces) this.supersede(taskId, replaces);

		// The ordinary "working on it" is the tool's pending message; a queue position or an offline core is said now.
		const unusual = (typeof submitted.queuedAhead === 'number' && submitted.queuedAhead > 0) || submitted.watcherOnline === false;
		if (unusual && typeof submitted.message === 'string') this.deps.notice?.(framedSystem(submitted.message));
		if (signal?.aborted) return framedSystem(ENDED.continuing);
		return new Promise<string>((resolve, reject) => {
			const waiter: Waiter = { resolve, reject };
			this.waiting.set(taskId, waiter);
			signal?.addEventListener('abort', () => {
				if (this.endCall(taskId, 'continuing'))
					this.deps.log?.(`[RelayAgent] ${taskId}: work call ended before its result; delivering it another way`);
			}, { once: true });
		});
	}

	/** A result landed. True when its call is waiting and now returns it. */
	offerResult(item: ResultItem): boolean {
		const taskId = item.taskId;
		if (!taskId) return false;
		const waiter = this.waiting.get(taskId);
		if (!waiter) return false;
		this.waiting.delete(taskId);
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

	/**
	 * The model said this task corrects an earlier request (`replaces`). The one `planSupersede` picks
	 * has its result left unsaid and its waiting call ended.
	 */
	private supersede(taskId: string, replaces: string): void {
		const now = Date.now();
		const plan = planSupersede({ taskId, replaces, rows: this.deps.store.list(), now });
		if (plan.action === 'none') {
			this.deps.log?.(plan.why === 'names_nothing'
				? `[RelayAgent] ${taskId}: "replaces" names no task or number (${replaces}); nothing corrected`
				: `[RelayAgent] ${taskId}: no unheard request carries "${replaces}"; nothing corrected`);
			return;
		}
		const { id } = plan;
		this.deps.store.setSupersededBy(id, taskId);
		this.endCall(id, 'delivered_separately');
		this.deps.log?.(`[RelayAgent] ${id}: corrected by ${taskId}; its result will not be said`);
	}

	/** Ends the call waiting on `taskId` without its result. False when none waits. */
	endCall(taskId: string, why: keyof typeof ENDED): boolean {
		const waiter = this.waiting.get(taskId);
		if (!waiter) return false;
		this.waiting.delete(taskId);
		waiter.resolve(framedSystem(ENDED[why]));
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
