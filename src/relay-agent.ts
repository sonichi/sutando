/**
 * Sutando's relay agent: the voice side's task manager between the voice model and the core, run
 * as the bodhi subagent behind the NON_BLOCKING `work` tool: each call stays open until its task's
 * result returns as that call's own response (WHEN_IDLE). It keeps a table of every task the user asked for by
 * voice (what, when, cancel asked, how its result reached the user, which result answered it) and
 * answers from it; a result whose call is gone is delivered another way and reconciled.
 */

import type { IEventBus, PersistentSubagentInstance, PersistentSubagentResult, SubagentConfig } from 'bodhi-realtime-agent';
import { frameTaskResult, framedSystem } from './inject-framing.js';
import type { HeardCheck, HeardInput, OpenTask, TurnClassifier, WorkCheck, WorkLane, WorkRequestCheck } from './work-request-check.js';

/** How a voice task's result reached the user. `injected`: handed to the model, turn never confirmed. */
export type Delivery = 'spoken' | 'injected' | 'dm';

/** One row per task the user asked for by voice. Where the task stands is read from the core, not stored here. */
export interface VoiceTaskRecord {
	text?: string;
	submittedAt?: number;
	/** Which worker lane it went to; quick tasks queue apart from long ones. */
	lane?: WorkLane;
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
	/** When the turn that told the user its result ended: a user turn after it asks again. Not `at`, which the heard
	 *  verdict moves up to seconds later (a re-check she said meanwhile read as asked before she heard it). */
	heardAt?: number;
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
		add(taskId: string, text: string, submittedAt: number = now(), lane?: WorkLane): void {
			update(taskId, (row) => ({ ...row, text: text.slice(0, 200), submittedAt, ...(lane ? { lane } : {}) }));
		},
		/** The user asked to cancel it. */
		markCancelRequested(taskId: string): void {
			update(taskId, (row) => ({ ...row, cancelRequested: true }));
		},
		/** How its result reached the user; for 'spoken', `heardAt` is when the turn that said it ended (now if not given). */
		set(taskId: string, delivery: Delivery, heardAt?: number): void {
			const told = delivery === 'spoken' ? { heardAt: heardAt ?? now() } : {};
			// A confirmed delivery is never downgraded by a later, weaker one.
			const deliver = (row: VoiceTaskRecord) => (row.delivery === 'spoken' && delivery !== 'spoken' ? null : { ...row, delivery, ...told });
			update(taskId, deliver);
			// One result answers this task and every task answered in it (or in the result this task was answered in):
			// whichever of them it reached the user through, it reached the user for all of them.
			const source = tasks.get(taskId)?.answeredBy ?? taskId;
			if (source !== taskId) update(source, deliver);
			for (const [id, row] of [...tasks.entries()]) if (id !== taskId && row.answeredBy === source) update(id, deliver);
		},
		/** A hand-over the session lost before it was spoken: reconcile hands it over again. */
		unsetHandedOver(taskId: string): void {
			update(taskId, (row) => (row.delivery === 'injected' ? { ...row, delivery: undefined } : null));
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
			const { delivery: reached, heardAt } = tasks.get(target) ?? {};
			update(taskId, (row) => ({ ...row, answeredBy: target, ...(reached && row.delivery !== 'spoken' ? { delivery: reached, ...(heardAt !== undefined ? { heardAt } : {}) } : {}) }));
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
	/** bodhi dropped the open start (a blip, a reset): no end follows. */
	onUserSpeechAborted(e?: SpeechEvent): void;
	/** The model started speaking to the user. */
	onModelAudio(): void;
	/**
	 * Resolves once the user's turn is over, or the hold limit or `signal` ends the wait. Model audio from the call on
	 * counts as the answer; audio from `answeredSince` on counts only if the user began speaking after it (cut in on it).
	 */
	waitTurnOver(signal?: AbortSignal, answeredSince?: number): Promise<void>;
	/** When the client's microphone last heard the user stop speaking (0 if never). */
	userEndedAt?(): number;
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
		// Not an end: the quiet time runs from the last real one (2026-10-21 17:42:45: a 43 ms blip held a work call to the 30 s cap).
		onUserSpeechAborted(e) { if (!fromClient(e)) return; speaking = false; wake(); },
		onModelAudio() { modelAudioAt = now(); wake(); },
		userEndedAt: () => endedAt ?? 0,
		waitTurnOver(signal, answeredSince) {
			const since = now();
			return new Promise<void>((resolve) => {
				let timer: ReturnType<typeof setTimeout> | null = null;
				const done = () => { waits.delete(look); if (timer) clearTimeout(timer); resolve(); };
				function look() {
					const t = now();
					if (signal?.aborted || t - since >= maxHoldMs) return done();
					// The model answered: the user's turn is over, whatever the microphone picks up now. Audio from before the
					// wait answered it only if she cut in on it; audio over a sentence she was still saying did not.
					if (modelAudioAt !== null && (modelAudioAt >= since || (answeredSince !== undefined && modelAudioAt >= answeredSince && startedAt > modelAudioAt))) return done();
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

/** What listens to the user's speech: the speech gate, the hand-off pacer. */
export interface UserSpeechSink {
	onUserSpeechStarted(e?: SpeechEvent): void;
	onUserSpeechEnded(e?: SpeechEvent): void;
	onUserSpeechAborted(e?: SpeechEvent): void;
}

/**
 * Subscribes a listener to every user speech event bodhi publishes. One place, so the gate and the pacer hear the
 * same events: speech.user_aborted was wired in neither, and a dropped start held both until their caps.
 */
export function wireUserSpeech(bus: Pick<IEventBus, 'subscribe'>, sink: UserSpeechSink): void {
	bus.subscribe('speech.user_started', (e) => sink.onUserSpeechStarted(e));
	bus.subscribe('speech.user_ended', (e) => sink.onUserSpeechEnded(e));
	bus.subscribe('speech.user_aborted', (e) => sink.onUserSpeechAborted(e));
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
		/** Ids of the results queued or on their way: a status answer does not read them out a second time. */
		held(): string[] { return [...(current ? [current.h] : []), ...queue].flatMap((h) => h.ids); },
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
		/** A start bodhi dropped (no end follows): the user is not speaking, and the reply wait still runs from the last real end (2026-10-21 17:46:25). */
		onUserSpeechAborted(e?: SpeechEvent): void {
			if (!fromClient(e)) return;
			userSpeaking = false;
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

/** An injected line, not speech: "[System: …]" from our framing, "[system] …" from bodhi's injectSystemMessage (stored as role assistant). */
const isInjected = (content: string) => content.startsWith('[System:') || content.startsWith('[system] ');

/** The last `n` spoken lines of the live conversation, oldest first, as "user: …" / "assistant: …". System lines are left out. */
export function recentConversationLines(items: ReadonlyArray<{ role?: string; content?: string }>, n: number): string {
	return items
		.filter((i) => (i.role === 'user' || i.role === 'assistant') && i.content?.trim() && !isInjected(i.content))
		.slice(-n)
		.map((i) => `${i.role}: ${i.content!.replace(/\s+/g, ' ').trim()}`)
		.join('\n');
}

const oneLine = (s: string) => s.replace(/\s+/g, ' ').trim();
const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
export const TURN_TOOL_LINE_MAX = 200;
const TURN_TOOL_ARGS_MAX = 120;

/** A tool result as "ok", "ok: …" or "error: …". */
function resultStatus(r: Record<string, unknown>): string {
	const out = r.result as unknown;
	const failed = out && typeof out === 'object' && (out as { ok?: unknown }).ok === false;
	return typeof r.error === 'string' && r.error ? `error: ${oneLine(r.error)}`
		: failed ? `error: ${String((out as { error?: unknown }).error ?? JSON.stringify(out))}`
		: out === null || out === undefined ? 'ok'
		: `ok: ${oneLine(typeof out === 'string' ? out : JSON.stringify(out))}`;
}

/**
 * What backs the assistant's words this turn: each tool call in it as "name {args} -> ok | error: … | no result yet",
 * and each result or notice delivered to the model as "delivered: …", a result for a call of an earlier turn included
 * (a `work` call's result comes back turns after the call). Each line at most TURN_TOOL_LINE_MAX chars. The turn check
 * compares the assistant's claims with it (2026-10-21 17:39:23: "I've replaced the V2 plan…" with no tool call).
 * The turn is what came after `since` (the end of the turn before) when the items are stamped: bodhi can write the
 * user's line after the turn's tools when the transcription arrives late. Otherwise it starts at the user's entry
 * (`userText`, else the last real user line).
 */
export function thisTurnToolLines(items: ReadonlyArray<{ role?: string; content?: string; timestamp?: number }>, userText?: string, since?: number): string {
	const isUser = (i: { role?: string; content?: string }) => i.role === 'user' && !!i.content?.trim() && !isInjected(i.content);
	let from: number;
	// The session's first turn (since 0) keeps the user-line start: items loaded from an earlier session are stamped too.
	if (since && typeof items[items.length - 1]?.timestamp === 'number') {
		from = items.findIndex((i) => typeof i.timestamp === 'number' && i.timestamp > since);
		if (from < 0) return '';
	} else {
		let start = -1;
		const want = userText?.trim();
		if (want) for (let k = items.length - 1; k >= 0 && start < 0; k--) if (isUser(items[k]) && items[k].content!.trim() === want) start = k;
		if (start < 0) for (let k = items.length - 1; k >= 0 && start < 0; k--) if (isUser(items[k])) start = k;
		if (start < 0) return '';
		from = start + 1;
	}
	const lines: Array<{ head: string; status: string }> = [];
	const byId = new Map<string, { head: string; status: string }>();
	const parse = (c: string | undefined): Record<string, unknown> | null => { try { const v = JSON.parse(c ?? ''); return v && typeof v === 'object' ? v : null; } catch { return null; } };
	for (const item of items.slice(from)) {
		const content = item.content ?? '';
		if (item.role === 'tool_call') {
			const c = parse(content);
			if (!c) continue;
			const line = { head: `${String(c.toolName ?? '?')} ${clip(JSON.stringify(c.args ?? {}), TURN_TOOL_ARGS_MAX)}`, status: 'no result yet' };
			lines.push(line);
			if (typeof c.toolCallId === 'string') byId.set(c.toolCallId, line);
		} else if (item.role === 'tool_result') {
			const r = parse(content);
			if (!r) continue;
			const line = typeof r.toolCallId === 'string' ? byId.get(r.toolCallId) : undefined;
			// A call from an earlier turn: its result reached the model now ("is it merged?" answered by it was called unbacked).
			if (line) line.status = resultStatus(r);
			else lines.push({ head: `delivered: ${String(r.toolName ?? '?')} result`, status: resultStatus(r) });
		} else if ((item.role === 'user' || item.role === 'assistant') && isInjected(content)) {
			lines.push({ head: `delivered: ${oneLine(content)}`, status: '' });
		}
	}
	return lines.map((l) => clip(l.status ? `${l.head} -> ${l.status}` : l.head, TURN_TOOL_LINE_MAX)).join('\n');
}

/** A question waiting for the model to stop speaking is put anyway after this long. */
export const ASK_WAIT_MAX_MS = 15_000;

/**
 * Puts the relay's question to the user as live input as soon as the model is not speaking: at once when it
 * is quiet, else when its turn ends (or after ASK_WAIT_MAX_MS). Only the newest question waits.
 */
export function createAskDelivery(deps: { inject: (text: string) => void; maxWaitMs?: number; log?: (m: string) => void }) {
	let speaking = false;
	// Held in order while the model speaks: a result and a nudge for another must both go, not the newest alone.
	const waiting: Array<{ text: string; onDelivered?: () => void }> = [];
	let timer: ReturnType<typeof setTimeout> | null = null;
	const flush = () => {
		if (timer) { clearTimeout(timer); timer = null; }
		for (const w of waiting.splice(0)) { deps.inject(w.text); w.onDelivered?.(); }
	};
	return {
		/** True when the text went to the model now; false when it waits for the turn to end (then `onDelivered` runs). */
		ask(text: string, onDelivered?: () => void): boolean {
			if (!speaking) { deps.inject(text); onDelivered?.(); return true; }
			waiting.push({ text, onDelivered });
			if (!timer) {
				timer = setTimeout(() => { deps.log?.('[RelayAgent] the model is still speaking; asking anyway'); flush(); }, deps.maxWaitMs ?? ASK_WAIT_MAX_MS);
				timer.unref?.();
			}
			return false;
		},
		onModelAudio(): void { speaking = true; },
		onTurnEnd(): void { speaking = false; flush(); },
	};
}

/** The model's turn a `work` call came in: whether the model's audio began in it, and when it ends. */
export interface TurnMark {
	readonly spoke: boolean;
	readonly ended: boolean;
	/** Resolves when the turn ends. */
	readonly whenEnded: Promise<void>;
}

/** Follows the model's turns (bodhi's turn.start, response.first_audio, turn.end) for the relay agent. */
export interface ModelTurnWatch {
	onTurnStart(): void;
	onModelAudio(): void;
	onTurnEnd(): void;
	/** The turn open now; null between turns. */
	current(): TurnMark | null;
}

export function createModelTurnWatch(): ModelTurnWatch {
	type Open = { spoke: boolean; ended: boolean; whenEnded: Promise<void>; end: () => void };
	let open: Open | null = null;
	const close = () => { if (open) { open.ended = true; open.end(); open = null; } };
	return {
		onTurnStart() {
			close();
			let end!: () => void;
			const whenEnded = new Promise<void>((r) => { end = r; });
			open = { spoke: false, ended: false, whenEnded, end };
		},
		onModelAudio() { if (open) open.spoke = true; },
		onTurnEnd: close,
		current: () => open,
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
	/** Adds a line to the model's context without asking for a reply (bodhi injectText 'quiet'). */
	context?: (text: string) => void;
	/** The model's turns: a call the model spoke in is not acknowledged again. Without it, every submit is acknowledged aloud. */
	modelTurns?: ModelTurnWatch;
	/** Holds each call until the user has finished speaking. Without it, calls are submitted at once. */
	speech?: SpeechGate;
	/** Decides, before a request is submitted, whether the user must first be asked for a missing piece. */
	check?: WorkRequestCheck;
	/** The recent conversation the check reads. */
	recentConversation?: () => string;
	/** Looks at every finished user turn, whatever tool the voice model chose: is it a request for the core? */
	classify?: TurnClassifier;
	/** Decides whether a spoken turn told the user a result waiting to be heard. Without it, the runtime's own rule decides. */
	heard?: HeardCheck;
	/** This turn's tool calls and delivered results (thisTurnToolLines): after `since`, the end of the turn before, whose user line is `userText`. */
	thisTurn?: (userText: string, since: number) => string;
	/** Has the voice model put a question to the user as soon as it stops speaking (falls back to `notice`). */
	ask?: (text: string) => void;
	/** Resolves when the session can take a reply: ACTIVE now, or at the next ACTIVE. A reply sent while a connection
	 *  is being replaced is dropped by the transport, so every reply of a call waits for it. */
	whenActive?: () => Promise<void>;
	/** A request entered or left the in-flight list (inFlightRequests): the status page is rewritten. */
	onInFlightChange?: () => void;
	log?: (msg: string) => void;
}

/** Where an accepted request stands before it is a task row: held for the user to finish, in the work check, or being submitted. */
export type InFlightStage = 'held' | 'checking' | 'sending';
export interface InFlightRequest { task: string; stage: InFlightStage }

/** After the user was asked about a request, the next call within this window is sent as it is. */
export const ONE_ASK_WINDOW_MS = 2 * 60_000;

/** Said right after a request reaches the core: the open `work` call returns its result later. */
/** A request already with the core: the new call waits for that result, and the user hears that it is on its way. */
export const DUPLICATE_WAITS = 'This request was already sent to the core and its result will be told when it comes. Tell the user so in one short sentence.';
export const WORK_ACKNOWLEDGE = 'The request was sent to the core and is being worked on; its result comes back later as the answer to that work call. Tell the user in one short sentence that you are on it, then carry on with the conversation. Do not call work again for it.';
/** The same, as context only: the model already told the user it is on it in the turn that made the call. */
export const WORK_SENT_QUIET = 'The request was sent to the core and is being worked on; its result comes back later as the answer to that work call. You already told the user you are on it; say nothing more about it until its result comes. Do not call work again for it.';
export const DUPLICATE_QUIET = 'This request was already sent to the core and its result will be told when it comes. You already told the user you are on it; say nothing more about it until its result comes. Do not call work again for it.';

/** A request sent this long ago and still unanswered is compared with new ones (a repeat is not sent twice). */
export const OPEN_TASK_WINDOW_MS = 30 * 60_000;

/** A task answered this recently is still compared with a new request: the model may re-issue a
 *  call seconds after its result, and that orphan is closed quietly instead of resent. */
export const RECENT_ANSWER_WINDOW_MS = 60_000;

interface Waiter {
	resolve: (text: string) => void;
	reject: (err: Error) => void;
}

/** How a waiting call ends without its result. bodhi reads a rejection out as a failure, so none rejects. */
const ENDED: Record<'cancelled' | 'continuing' | 'delivered_separately' | 'continued_in_next' | 'not_sent' | 'already_answered' | 'say_now', string> = {
	not_sent: 'This call was cancelled before the request was sent; nothing was submitted.',
	already_answered: 'This request was answered a moment ago; nothing new to say. Stay silent about it.',
	say_now: 'The result for this request already came back through its earlier call. Say it to the user now, in a sentence or two; nothing was resent.',
	continued_in_next: 'The user was not finished: this request was sent together with what they said next. Say nothing about it.',
	cancelled: 'The user cancelled this task and was already told. Do not mention it again.',
	continuing: 'The task is still running; its result will come in a separate message.',
	delivered_separately: 'This task\'s result is delivered to the user separately. Say nothing about it now.',
};

/** Two calls joined into one request: the model spoke for it if it spoke in either call's turn; it ends with the later one. */
function eitherSpoke(a: TurnMark | null, b: TurnMark | null): TurnMark | null {
	if (!a || !b) return a ?? b;
	return { get spoke() { return a.spoke || b.spoke; }, get ended() { return b.ended; }, whenEnded: b.whenEnded };
}

/**
 * The `work` subagent. `invoke` submits the task and, unless the status is the answer (a duplicate,
 * a rejection, the fast path), waits for the task's result and returns it. `offerResult` hands a
 * landed result to its waiting call; it refuses (the caller delivers another way) when no call waits.
 */
export class RelayAgent<O = unknown> implements PersistentSubagentInstance {
	readonly key = 'relay-agent';
	private readonly waiting = new Map<string, Waiter>();
	/** The call waiting for the user to finish speaking, not yet submitted. */
	private holding: { args: Record<string, unknown>; origin: O | undefined; joined: boolean; flight: InFlightRequest; mark: TurnMark | null } | null = null;
	/** Requests accepted and not yet a task row: the store has no row for them until the submit returns. */
	private readonly inFlight = new Set<InFlightRequest>();

	/** When the voice model last took a turn itself (`work`, or a tool that fetches the answer): the turn check does not send it again. */
	private lastTakenAt = 0;
	/** When the voice model last called `work`: a turn it sent is never judged again, whatever else it called. */
	private lastWorkAt = 0;
	/** When the voice model last wrote (a page append): that turn is judged even if a read also answered it. */
	private lastWroteAt = 0;
	/** When the work check last had the user asked: the next call's answer is sent as it is. */
	/** When the user last finished a turn: a re-issued call before any user turn is an orphan, after one it is a repeat. */
	private lastUserTurnAt = 0;
	/** Fresh dials so far; a call compares it with the count when it began. */
	private boundaries = 0;
	private askedAt = 0;
	/** User turns waiting for the turn check (several when the user went on speaking). */
	private turns: Array<{ text: string; at: number; origin: O | undefined }> = [];
	private turnsRunning = false;
	/** When the last turn the turn check took ended: a `work` call after it belongs to the next turn. */
	private lastTurnAt = 0;
	/** Tasks the user was told are being worked on: a correction of one is not acknowledged again. */
	private readonly acknowledged = new Set<string>();
	/** Returned results whose heard verdict is still coming (heardPending): settled once it is decided. */
	private readonly heardVerdicts = new Map<string, Promise<void>>();

	constructor(private readonly deps: RelayAgentDeps<O>) {}

	/**
	 * Requests accepted and not yet sent, which status answers count as pending (nbe2e 2026-10-21 17:13:24: the
	 * video request was held for the user and then in the check, no row existed, and get_task_status said
	 * "pending 0"). A joined call is one entry; an entry leaves as soon as its submit returns, when the row exists.
	 */
	inFlightRequests(): InFlightRequest[] {
		return [...this.inFlight].map((e) => ({ ...e }));
	}

	private enterFlight(e: InFlightRequest): void {
		this.inFlight.add(e);
		this.deps.onInFlightChange?.();
	}

	private leaveFlight(e: InFlightRequest): void {
		if (this.inFlight.delete(e)) this.deps.onInFlightChange?.();
	}

	private flightStage(e: InFlightRequest, stage: InFlightStage): void {
		if (e.stage === stage) return;
		e.stage = stage;
		if (this.inFlight.has(e)) this.deps.onInFlightChange?.();
	}

	/** Tasks sent and not answered yet, newest last: what a new request is compared with. */
	openTasks(now = Date.now()): OpenTask[] {
		return this.deps.store.list()
			.filter(([, r]) => !!r.text && !r.delivery && !r.cancelRequested && !r.retired && !r.supersededBy && !r.answeredBy && (r.submittedAt ?? r.at) > now - OPEN_TASK_WINDOW_MS)
			.map(([id, r]) => ({ id, text: r.text as string }));
	}

	/** Tasks answered in this session within RECENT_ANSWER_WINDOW_MS (not DM'd): a re-issued call for one is an orphan. */
	recentlyAnswered(now = Date.now()): OpenTask[] {
		return this.deps.store.list()
			.filter(([, r]) => !!r.text && (r.delivery === 'injected' || r.delivery === 'spoken') && r.at > now - RECENT_ANSWER_WINDOW_MS)
			.map(([id, r]) => ({ id, text: r.text as string }));
	}

	/** A fresh dial (bodhi's `recoverUpstream`, published as `session.reconnectBoundary`) abandons the old
	 *  connection's calls: every waiting call ends and its result goes another way. A resume keeps them. A call
	 *  still in its checks when the boundary passed ends when it would start waiting. */
	noteBoundary(): string[] {
		this.boundaries++;
		return this.endAllCalls('continuing');
	}

	/** Every waiting call ends without its result, which is then delivered another way. */
	endAllCalls(why: keyof typeof ENDED): string[] {
		const ended = [...this.waiting.keys()];
		for (const id of ended) this.endCall(id, why);
		return ended;
	}

	/**
	 * The user finished a turn. The turn check decides whether it asks the core for something, whichever
	 * tool the voice model chose (2026-10-12: "帮我查一下 5334 的状态" went to get_task_status and was lost).
	 * A turn the voice model already sent with `work` is left to that call.
	 */
	onUserTurn(text: string, at = Date.now()): void {
		const t = text.trim();
		if (t && !t.startsWith('[System:')) this.lastUserTurnAt = at;
		if (!this.deps.classify || !t || t.startsWith('[System:')) return;
		this.turns.push({ text: t, at, origin: this.deps.captureOrigin?.() });
		if (!this.turnsRunning) void this.drainTurns();
	}

	private async drainTurns(): Promise<void> {
		this.turnsRunning = true;
		try {
			while (this.turns.length) {
				// One request may span several turns: wait until the user is done, then take them together.
				// A reply the model began since the previous turn completed, and the user then cut in on, answers this
				// one: a turn reaches the check only when it completes, after the reply began, so audio after the wait
				// began may never come (2026-10-21 17:38:45: the check waited the 30 s cap for her to stop). A reply
				// over a sentence she is still saying does not end it.
				if (this.deps.speech) await this.deps.speech.waitTurnOver(undefined, this.lastTurnAt || undefined);
				const taken = this.turns.splice(0);
				const since = this.lastTurnAt;
				this.lastTurnAt = taken[taken.length - 1].at;
				if (this.tookTurn(since)) continue; // the voice model took it itself
				const openTasks = this.openTasks();
				const verdict = await this.deps.classify!({
					utterance: taken.map((x) => x.text).join(' '),
					recentConversation: this.deps.recentConversation?.() ?? '',
					openTasks,
					thisTurn: this.deps.thisTurn?.(taken[0].text, since) ?? '',
				}).catch(() => ({ action: 'none' as const }));
				if (this.tookTurn(since)) continue; // the voice model's own call came in while the check ran
				// Judged early, said once she is quiet: a line put in at turn end talks over her. A newer turn is judged
				// with her full words instead.
				if ((verdict.action === 'ask' || verdict.action === 'unbacked') && this.deps.speech) {
					await this.deps.speech.waitTurnOver();
					if (this.turns.length || this.tookTurn(since)) {
						this.deps.log?.(`[RelayAgent] turn check: the user went on; not asking about "${taken.map((x) => x.text).join(' ')}"`);
						continue;
					}
				}
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
		if (verdict.action === 'unbacked') {
			// 2026-10-21 17:39:23 / 17:40:00: the user heard "replaced", "created" and nothing had been done.
			const claim = verdict.claim.replace(/"/g, "'");
			this.deps.log?.(`[RelayAgent] the voice model said "${claim}" and no tool did it; telling it`);
			const sent = verdict.task ? !!(await this.submitFromTurn(verdict.task, origin)) : false;
			const line = sent && verdict.task
				? framedSystem(`You told the user "${claim}", but nothing had done it. It has now been sent to the core: "${verdict.task.replace(/"/g, "'")}". Tell the user in one short sentence that it is not done yet and is being worked on now. Do not call work for it.`)
				// Its own tool's usual steps: a room write is still said back and confirmed (review: no write without a yes).
				: framedSystem(`You told the user "${claim}", but no tool did that this turn. Tell the user in one short sentence that it is not done yet, then do it now: with your own tool, following its usual steps (a write to a room is said back first and done after a clear yes), or with work when only the core can do it.`);
			(this.deps.ask ?? this.deps.notice)?.(line);
			return;
		}
		const taskId = await this.submitFromTurn(verdict.task, origin);
		if (!taskId) return;
		this.acknowledged.add(taskId);
		// Its result comes like any other with no call waiting: handed to the model when it can speak.
		const task = verdict.task.replace(/"/g, "'");
		// One acknowledgement (as for a work call): the model already said it is on it, so it is told as context only
		// (2026-10-21 18:38:35 "Understood, I'll make sure to check…", then 18:38:41 "I'm looking into those pull requests now.").
		// A correction of a request the user already heard acknowledged is the same request (18:56:16 "c h i s o n a c h i",
		// answered "Mm-hm.", then 18:56:22 "I'm currently looking into those pull requests").
		const corrected = !!verdict.corrects && this.acknowledged.has(verdict.corrects);
		if (corrected) this.deps.log?.(`[RelayAgent] ${taskId}: corrects ${verdict.corrects}, already acknowledged; told as context`);
		if ((verdict.acknowledged || corrected) && this.deps.context) this.deps.context(framedSystem(`This request was sent to the core for the user: "${task}". You already told the user you are on it; say nothing more about it until its result comes. Do not call work for it.`));
		else this.deps.notice?.(framedSystem(`This request was sent to the core for the user: "${task}". Tell the user in one short sentence that it is being looked into. Do not call work for it.`));
	}

	/** Submits a request the turn check wrote. Its task id when the core took it. */
	private async submitFromTurn(task: string, origin: O | undefined): Promise<string | null> {
		// Pending while it is submitted, like a work call's request: the row exists only once the submit returns.
		const flight: InFlightRequest = { task, stage: 'sending' };
		this.enterFlight(flight);
		let submitted: Record<string, unknown>;
		try {
			submitted = await this.deps.submit({ task }, origin);
		} finally {
			this.leaveFlight(flight);
		}
		const taskId = submitted.taskId;
		if (submitted.status !== 'pending' || typeof taskId !== 'string') {
			this.deps.log?.(`[RelayAgent] turn check: the request was not taken (${String(submitted.status)})`);
			return null;
		}
		this.deps.log?.(`[RelayAgent] ${taskId}: sent by the turn check (the voice model had not called work): ${task}`);
		return taskId;
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

	/**
	 * The voice model wrote something (write_page). Not an answer: asked to replace a page, it appended to it
	 * (2026-10-21 17:36:55, 17:39:35), so the turn check still judges the turn, even after a read in it.
	 */
	noteWritingTool(name: string): void {
		this.lastWroteAt = Date.now();
		this.deps.log?.(`[RelayAgent] ${name} writes in this turn; the turn check still judges it`);
	}

	/** The voice model took the turn after `since`: its own work call, or an answering tool in a turn that wrote nothing. */
	private tookTurn(since: number): boolean {
		return this.lastWorkAt > since || (this.lastTakenAt > since && !(this.lastWroteAt > since));
	}

	/** What bodhi calls for the `work` tool: the answer and how it is delivered (a plain answer goes when_idle). */
	async invokeWithScheduling(task: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<PersistentSubagentResult> {
		const out = await this.run(task, args, signal);
		// A reply produced while the connection is being replaced never reaches the model: it waits for ACTIVE.
		if (this.deps.whenActive) await this.deps.whenActive();
		return typeof out === 'string' ? { text: out } : out;
	}

	async invoke(task: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
		return (await this.invokeWithScheduling(task, args, signal)).text;
	}

	private async run(_task: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<string | PersistentSubagentResult> {
		// Listed from the moment the call is accepted until its submit returns or it ends without one. A returned
		// promise (a call that waits for its result) is not awaited here, so the finally runs as the call starts waiting.
		const flight: InFlightRequest = { task: String(args.task ?? ''), stage: this.deps.speech ? 'held' : 'checking' };
		this.enterFlight(flight);
		try {
			this.lastTakenAt = this.lastWorkAt = Date.now();
			const boundary = this.boundaries;
			// The turn this call came in: if the model speaks in it, that is the acknowledgement (turn.start comes with
			// the model's response, so a call arrives inside its turn; none open, nothing it said can be about this call).
			let mark = this.deps.modelTurns?.current() ?? null;
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
					mark = eitherSpoke(earlier.mark, mark); // either call's turn may have said it is on it
					args = {
						...earlier.args,
						...args,
						task: joinCutOffRequest(String(earlier.args.task ?? ''), String(args.task ?? '')),
					};
					// One request from now on: the earlier call's entry goes, this one carries the joined text.
					this.leaveFlight(earlier.flight);
					flight.task = String(args.task);
					this.deps.log?.('[RelayAgent] the user went on speaking: the earlier request is sent with this one');
				}
				const mine = { args, origin, joined: false, flight, mark };
				this.holding = mine;
				await this.deps.speech.waitTurnOver(signal);
				// Closed without a turn: the later call's acknowledgement is the one (2026-10-21 17:12:25 nbe2e: this answer,
				// sent when_idle, was answered "I'm looking into those" before the later call was even submitted).
				if (mine.joined) return { text: framedSystem(ENDED.continued_in_next), scheduling: 'silent' };
				if (this.holding === mine) this.holding = null;
			}
			// bodhi cancelled the call (tool cancellation, the session closing) while it waited for the user or the check.
			if (signal?.aborted) return framedSystem(ENDED.not_sent);
			let lane: WorkLane | undefined;
			if (this.deps.check) {
				this.flightStage(flight, 'checking');
				// A request with a blank in it (which PR? which file?) is not submitted: the user is asked.
				const verdict = await this.deps.check({ task: String(args.task ?? ''), recentConversation: this.deps.recentConversation?.() ?? '', openTasks: this.openTasks(), answeredTasks: this.recentlyAnswered() }, signal)
					.catch((): WorkCheck => ({ action: 'submit' }));
				if (signal?.aborted) return framedSystem(ENDED.not_sent);
				if (verdict.action === 'submit') lane = verdict.lane;
				// One question per request: the answer is sent as it is, even if the check still finds it vague
				// ("the latest one") - the user is not interrogated (review of #5348).
				if (verdict.action === 'ask' && Date.now() - this.askedAt < ONE_ASK_WINDOW_MS) {
					this.deps.log?.(`[RelayAgent] the user was already asked about this; sending it as it is`);
				} else if (verdict.action === 'ask') {
					this.askedAt = Date.now();
					this.deps.log?.(`[RelayAgent] not submitted, asking the user: ${verdict.question}`);
					return framedSystem(`Nothing was submitted: the request does not say what it is about. Ask the user now, in their language, exactly: "${verdict.question.replace(/"/g, "'")}" Then, with their answer, call work again.`);
				}
				if (verdict.action === 'duplicate' && this.deps.store.get(verdict.of)?.delivery) {
					// Returned, and the turn that may have said it still being judged: its verdict decides which case this is
					// (bounded by the runtime's wait). Told meanwhile, "say it now" would have it said twice.
					const pending = this.deps.store.get(verdict.of)!.delivery === 'injected' ? this.heardVerdicts.get(verdict.of) : undefined;
					if (pending) {
						this.deps.log?.(`[RelayAgent] ${verdict.of}: asked again while its heard verdict is coming; waiting for it`);
						await pending;
						if (signal?.aborted) return framedSystem(ENDED.not_sent);
					}
					const row = this.deps.store.get(verdict.of)!;
					// Said already and no user turn since: the model re-issued its own call, an orphan. Closed quietly. Her speech
					// counts as her turn: her line is logged only when the turn answering it ends, after this call (2026-10-21
					// 18:56:55: "I think I merged more than that" would have been closed silently).
					const userAt = Math.max(this.lastUserTurnAt, this.deps.speech?.userEndedAt?.() ?? 0);
					if (row.delivery === 'spoken' && (row.heardAt ?? row.at) > userAt) {
						this.deps.log?.(`[RelayAgent] ${verdict.of}: asked again after its result was said; closed without resending`);
						return framedSystem(ENDED.already_answered);
					}
					// Returned to its call but not said yet: the answer is there; the model says it instead of waiting for the nudge.
					if (row.delivery === 'injected') {
						this.deps.log?.(`[RelayAgent] ${verdict.of}: asked again before its result was said; the model is told to say it`);
						return framedSystem(ENDED.say_now);
					}
					// Answered, and the user asked again since ("check it again"): a new request.
					this.deps.log?.(`[RelayAgent] ${verdict.of}: asked again after a user turn; sent as a new request`);
				} else if (verdict.action === 'duplicate') {
					// The same request is already with the core. This call waits for that task's result instead,
					// unless another call already does; either way the user is told it is on its way.
					this.deps.log?.(`[RelayAgent] ${verdict.of}: asked again; not sent twice`);
					// Answered at once: the answer is the acknowledgement (a notice as well was said a second time), unless
					// the model already said it in the call's turn: then the answer takes no turn of its own.
					if (this.waiting.has(verdict.of)) {
						if (mark && await this.spokeIn(mark)) return { text: framedSystem(DUPLICATE_QUIET), scheduling: 'silent' };
						return framedSystem(DUPLICATE_WAITS);
					}
					if (this.boundaries !== boundary || !mark) this.deps.notice?.(framedSystem(DUPLICATE_WAITS));
					if (this.boundaries !== boundary) return framedSystem(ENDED.continuing); // the connection was replaced meanwhile
					const waits = new Promise<string>((resolve, reject) => {
						this.waiting.set(verdict.of, { resolve, reject });
						signal?.addEventListener('abort', () => { this.endCall(verdict.of, 'continuing'); }, { once: true });
					});
					if (mark) void this.acknowledgeOnce(verdict.of, mark, DUPLICATE_WAITS, DUPLICATE_QUIET);
					return waits;
				}
			}
			this.askedAt = 0; // this request goes; the next vague one may be asked about again
			// The lane is the check's judgement alone: one the voice model wrote into its call is dropped.
			const { lane: _fromModel, ...ownArgs } = args as Record<string, unknown> & { lane?: unknown };
			this.flightStage(flight, 'sending');
			const submitted = await this.deps.submit(lane ? { ...ownArgs, lane } : ownArgs, origin);
			// The row was added before the submit returned, with no await between: counted once, as the row.
			this.leaveFlight(flight);
			const taskId = submitted.taskId;
			if (submitted.status !== 'pending' || typeof taskId !== 'string') return JSON.stringify(submitted);
			const replaces = typeof args.replaces === 'string' ? args.replaces.trim() : '';
			if (replaces) this.supersede(taskId, replaces);

			// The call stays open (NON_BLOCKING), so a separate notice acknowledges it, unless the model already said it
			// is on it in the call's turn. Sent only once the checks let it through; a queue position or offline core instead.
			if (signal?.aborted) return framedSystem(ENDED.continuing);
			if (this.boundaries !== boundary) {
				// The connection was replaced while this call was in its checks: a call id from the old one.
				this.deps.log?.(`[RelayAgent] ${taskId}: the connection was replaced before this call could wait; its result goes another way`);
				return framedSystem(ENDED.continuing);
			}
			const unusual = (typeof submitted.queuedAhead === 'number' && submitted.queuedAhead > 0) || submitted.watcherOnline === false;
			this.acknowledged.add(taskId);
			// A queue position or an offline core is news for the user, said whatever the model said already.
			const spokenNow = unusual && typeof submitted.message === 'string' ? submitted.message : !mark ? WORK_ACKNOWLEDGE : null;
			if (spokenNow) this.deps.notice?.(framedSystem(spokenNow));
			const waits = new Promise<string>((resolve, reject) => {
				const waiter: Waiter = { resolve, reject };
				this.waiting.set(taskId, waiter);
				signal?.addEventListener('abort', () => {
					if (this.endCall(taskId, 'continuing'))
						this.deps.log?.(`[RelayAgent] ${taskId}: work call ended before its result; delivering it another way`);
				}, { once: true });
			});
			if (!spokenNow && mark) void this.acknowledgeOnce(taskId, mark, WORK_ACKNOWLEDGE, WORK_SENT_QUIET);
			return waits;
		} finally {
			this.leaveFlight(flight);
		}
	}

	/**
	 * Whether the model spoke in the call's turn, decided once that turn is over (or after ASK_WAIT_MAX_MS). Any speech
	 * counts, even about something else: no verdict on what it said exists here (the turn check skips a turn the
	 * model's own work call took), and a second "I'm on it" is the louder failure (2026-10-21 18:56:42).
	 */
	private async spokeIn(mark: TurnMark): Promise<boolean> {
		if (!mark.ended) {
			let timer: ReturnType<typeof setTimeout> | undefined;
			await Promise.race([mark.whenEnded, new Promise<void>((r) => { timer = setTimeout(r, ASK_WAIT_MAX_MS); timer.unref?.(); })]);
			clearTimeout(timer);
		}
		return mark.spoke;
	}

	/**
	 * One acknowledgement per request (2026-10-21 18:56:42 "Let me check your recent merge history for you.", then the
	 * notice's 18:56:45 "I'm checking your recent merge activity now."). Decided when the call's turn is over, so no line
	 * goes in while the model speaks: the model spoke in it, and the submit is context only; else one spoken notice.
	 * A result already returned to the call needs neither.
	 */
	private async acknowledgeOnce(taskId: string, mark: TurnMark, spoken: string, quiet: string): Promise<void> {
		const spoke = await this.spokeIn(mark);
		if (!this.waiting.has(taskId)) {
			this.deps.log?.(`[RelayAgent] ${taskId}: its call already ended; not acknowledged`);
			return;
		}
		if (spoke && this.deps.context) {
			this.deps.log?.(`[RelayAgent] ${taskId}: the model said it is on it in the call's turn; told as context`);
			this.deps.context(framedSystem(quiet));
			return;
		}
		this.deps.notice?.(framedSystem(spoken));
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

	/** Whether a model decides if a turn told a waiting result (`judgeHeard`). */
	get judgesHeard(): boolean {
		return !!this.deps.heard;
	}

	/** The ids of the waiting results the assistant's words told the user; null on no verdict, a failure or no check. */
	async judgeHeard(input: HeardInput): Promise<string[] | null> {
		if (!this.deps.heard) return null;
		try {
			return await this.deps.heard(input);
		} catch (err) {
			this.deps.log?.(`[RelayAgent] heard check failed: ${err instanceof Error ? err.message : String(err)}`);
			return null;
		}
	}

	/** A heard verdict for these returned tasks is coming; `decided` settles once it is in (or the rule decided). */
	heardPending(taskIds: string[], decided: Promise<void>): void {
		for (const id of taskIds) this.heardVerdicts.set(id, decided);
		void decided.finally(() => { for (const id of taskIds) if (this.heardVerdicts.get(id) === decided) this.heardVerdicts.delete(id); });
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
