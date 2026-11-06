/**
 * LiveAgentRuntime — step 5a-2 of the interaction-planes refactor.
 *
 * The reusable orchestration around a live session (design doc §5). bodhi's
 * VoiceSession is the session/transport engine; what Sutando adds — and what
 * every live adapter (web voice today; phone in 5b-2; MatrixRTC later) needs
 * identically — is the wiring between the DURABLE WORK CHANNEL (tasks/,
 * results/, context drops, ambient note state) and the live session's
 * injection path. That wiring moves here VERBATIM from voice-agent.ts
 * main(); voice-agent becomes a caller.
 *
 * The three-channel rule (design doc): media frames never touch this module;
 * it carries only control/durable-work traffic into the session.
 */

import { writeFileSync } from 'node:fs';
import type { VoiceSession } from 'bodhi-realtime-agent';
import { resolveWorkspace, statusPath } from './workspace_default.js';
import { injectText } from './browser-tools.js';
import { meetingHoldsModel } from './meeting-input-hold.js';
import { frameContextDrop, frameNoteViewMetadata, frameNoteViewFull, framedSystem } from './inject-framing.js';
import { deliverWithRetry } from './inject-delivery.js';
import { startResultWatcher, startContextDropWatcher, startNoteViewingWatcher, publishResultFile, voiceTaskStore, reconcileVoiceTasks, forwardOfflineVoiceResult, notPickedStillTrue } from './task-bridge.js';
import { createHandoffPacer, frameResult, resultIsMoot, type Handoff, type RelayAgent, type ResultItem } from './relay-agent.js';

const WORKSPACE_DIR = resolveWorkspace();

function ts(): string { return new Date().toISOString().slice(11, 23); }

/** Adapter-specific hooks for the durable-channel wiring. Everything here is
 * optional except the session: an adapter without a Cartesia fallback simply
 * omits it and the stuck-session path degrades to the Discord DM file. */
export interface DurableChannelOptions {
	/** Cartesia fallback for stuck sessions (web voice provides these;
	 * other adapters may not). */
	cartesiaApiKey?: string;
	generateSpeech?: ((text: string, meta: { category: string; label: string }) => Promise<string>) | null;
	/** Waits between checks while the session cannot take a result, before the DM fallback. */
	notReadyRetriesMs?: number[];
	/** How often the relay agent reconciles the task table with the core. */
	reconcileMs?: number;
	/** How long a handed-over result waits for the model's turn before the next one goes. */
	handoffConfirmMs?: number;
	/** A result returned through its call and not said by then is nudged once (a notice), then handed over again. */
	returnNudgeMs?: number;
	returnReleaseMs?: number;
	/** The `work` subagent, which owns the result queue. */
	relay: RelayAgent;
	/** Puts text to the model as live input once it is not speaking. Used for a result when system notices
	 *  are not getting through (after a reconnect, or once a notice went unheard). */
	injectLive?: (text: string, onDelivered?: () => void) => boolean | void;
}

/** A returned result not said by then is nudged once, then handed over again. */
/** Results the hand-off pacer still holds for the model: a status answer must not read them out a second time. */
let pacerHeld: () => string[] = () => [];
export const heldByPacer = (): string[] => pacerHeld();
export const RETURN_NUDGE_MS = 15_000;
export const RETURN_RELEASE_MS = 60_000;

const EVIDENCE_STOP = new Set(['about', 'after', 'again', 'already', 'also', 'because', 'before', 'being', 'could', 'every', 'first', 'found', 'from', 'have', 'here', 'into', 'just', 'last', 'more', 'most', 'other', 'over', 'ready', 'result', 'results', 'said', 'should', 'since', 'some', 'than', 'that', 'their', 'them', 'then', 'there', 'these', 'they', 'this', 'those', 'through', 'under', 'until', 'very', 'were', 'what', 'when', 'where', 'which', 'while', 'will', 'with', 'would', 'your', 'task', 'done', 'request', 'user']);
export interface ResultEvidence { numbers: string[]; words: string[] }
const CJK = /[一-鿿]/;
const cjkGrams = (run: string): string[] => { const out: string[] = []; for (let i = 0; i + 2 <= run.length; i++) out.push(run.slice(i, i + 2)); return out; };
const evidenceOf = (text: string): ResultEvidence => {
	const plain = text
		.replace(/\[(file|send|attach|thread|channel|dm-only|no-send|REPLIED)[^\]]*\]/gi, ' ')
		.replace(/\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\b/g, ' ') // a date's month or day is not the result
		.replace(/\b\d{1,2}:\d{2}\b/g, ' ');
	// A number under 13 is too common a word ("one", "two", "ten") to prove anything on its own.
	const numbers = [...new Set((plain.match(/\d+(?:[.,]\d+)?/g) ?? []).filter((n) => n.includes('.') || n.includes(',') || Number(n) >= 13))];
	const words = new Set<string>();
	for (const w of plain.toLowerCase().match(/[a-z][a-z'-]{3,}/g) ?? []) { const k = w.replace(/'s$|'$/, ''); if (k.length >= 4 && !EVIDENCE_STOP.has(k)) words.add(k); }
	for (const run of plain.match(/[一-鿿]{2,}/g) ?? []) for (const g of cjkGrams(run)) words.add(g); // CJK words: 2-character n-grams
	return { numbers, words: [...words] };
};
/** What a spoken turn must carry to count as the result: its numbers and its distinctive words, minus whatever
 *  it shares with the request itself (the PR number, the thing asked about): restating the request is not the answer. */
export function resultEvidence(text: string, request = ''): ResultEvidence {
	const own = evidenceOf(text);
	if (!request) return own;
	const asked = evidenceOf(request);
	return { numbers: own.numbers.filter((n) => !asked.numbers.includes(n)), words: own.words.filter((w) => !asked.words.includes(w)) };
}

const ONES = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'];
const TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'];
const CN_DIGITS = '零一二三四五六七八九';
const CN_NUMERAL = /[零一二三四五六七八九两十百千万点]/;
function englishBelowThousand(n: number, and = false): string {
	if (n < 20) return ONES[n];
	if (n < 100) return n % 10 ? `${TENS[Math.floor(n / 10)]} ${ONES[n % 10]}` : TENS[Math.floor(n / 10)];
	const rest = n % 100 ? ` ${and ? 'and ' : ''}${englishBelowThousand(n % 100)}` : '';
	return `${ONES[Math.floor(n / 100)]} hundred${rest}`;
}
function englishCardinal(n: number, and = false): string {
	if (n < 1000) return englishBelowThousand(n, and);
	if (n < 1_000_000) return `${englishBelowThousand(Math.floor(n / 1000), and)} thousand${n % 1000 ? ` ${n % 1000 < 100 && and ? 'and ' : ''}${englishBelowThousand(n % 1000, and)}` : ''}`;
	return '';
}
function chineseCardinal(n: number, liang = false): string {
	if (n < 10) return CN_DIGITS[n];
	if (n >= 100_000) return '';
	const units = ['', '十', '百', '千', '万'];
	const digits = String(n).split('').map(Number);
	let out = '';
	digits.forEach((d, i) => {
		const unit = units[digits.length - 1 - i];
		if (d === 0) { if (!out.endsWith('零') && i < digits.length - 1) out += '零'; return; }
		const two = liang && d === 2 && (unit === '百' || unit === '千' || unit === '万');
		out += (d === 1 && unit === '十' && i === 0 ? '' : two ? '两' : CN_DIGITS[d]) + unit;
	});
	return out.replace(/零+$/, '');
}
const squash = (s: string) => s.toLowerCase().replace(/[^a-z0-9一-鿿]+/g, '');
/** How a transcript may spell a number the result wrote in digits: "ninety-eight", "ninetyeight", "one hundred and
 *  twenty", "a hundred", "fifty-seven seventeen" (four digits read in pairs), "two oh five", "九十八", "两百五十". */
export function spokenNumberForms(num: string): string[] {
	const forms: string[] = [];
	const [whole, frac] = num.replace(/,/g, '').split('.');
	const n = Number(whole);
	if (!Number.isFinite(n) || whole.length > 6) return forms;
	const fracWords = (frac ?? '').split('').map((d) => ONES[Number(d)]);
	const tail = frac ? ` point ${fracWords.join(' ')}` : '';
	const cnTail = frac ? `点${frac.split('').map((d) => CN_DIGITS[Number(d)]).join('')}` : '';
	for (const and of [false, true]) {
		const en = englishCardinal(n, and);
		if (en && !forms.includes(en + tail)) forms.push(en + tail);
		if (en.startsWith('one hundred') || en.startsWith('one thousand')) forms.push(en.replace(/^one /, 'a ') + tail);
	}
	for (const liang of [false, true]) { const cn = chineseCardinal(n, liang); if (cn && !forms.includes(cn + cnTail)) forms.push(cn + cnTail); }
	if (frac && frac.length === 2 && n < 1000) forms.push(`${englishBelowThousand(n)} ${englishBelowThousand(Number(frac))}`); // "three fifty"
	if (!frac && whole.length === 4) {
		if (whole[2] !== '0') forms.push(`${englishBelowThousand(Number(whole.slice(0, 2)))} ${englishBelowThousand(Number(whole.slice(2)))}`); // "fifty-seven seventeen", never 2005
		if (whole.endsWith('00')) forms.push(`${englishBelowThousand(Number(whole.slice(0, 2)))} hundred`); // "twenty-five hundred"
	}
	if (!frac && whole.length >= 3) {
		forms.push(whole.split('').map((d) => ONES[Number(d)]).join(' '));
		forms.push(whole.split('').map((d) => (d === '0' ? 'oh' : ONES[Number(d)])).join(' '));
		forms.push(whole.split('').map((d) => CN_DIGITS[Number(d)]).join(''));
	}
	return [...new Set(forms.map((f) => f.trim()).filter(Boolean))];
}
/** A spelled form is said when its words are consecutive words of the turn (a join inside the number, "ninetyeight",
 *  is allowed; a join with a neighbouring word is not). A Chinese form must not sit inside a longer numeral. */
function turnSaysForm(tokens: string[], cjkRuns: string[], form: string): boolean {
	if (CJK.test(form)) {
		return cjkRuns.some((run) => { const i = run.indexOf(form); return i >= 0 && !(i > 0 && CN_NUMERAL.test(run[i - 1])) && !(i + form.length < run.length && CN_NUMERAL.test(run[i + form.length])); });
	}
	const target = squash(form);
	const parts = form.split(/\s+/).length;
	for (let i = 0; i < tokens.length; i++) {
		let joined = '';
		for (let k = 0; k < parts && i + k < tokens.length; k++) {
			joined += tokens[i + k];
			if (joined === target) return true;
			if (!target.startsWith(joined)) break;
		}
	}
	return false;
}
/** The turn said the result: one of its numbers (as digits, or as the transcript spells them); else its distinctive
 *  words, two of them when the result has numbers it did not say or has that many. No evidence at all: never by content. */
export function turnSaysResult(turn: string, evidence: ResultEvidence): boolean {
	if (!evidence.numbers.length && !evidence.words.length) return false;
	const lower = turn.toLowerCase();
	const tokens: string[] = lower.match(/[a-z0-9]+/g) ?? [];
	const cjkRuns: string[] = turn.match(/[一-鿿]+/g) ?? [];
	for (const n of evidence.numbers) {
		const digits = n.replace(/,/g, '');
		if (tokens.includes(digits) || tokens.includes(digits.replace('.', ''))) return true;
		if (spokenNumberForms(n).some((f) => turnSaysForm(tokens, cjkRuns, f))) return true;
	}
	const need = evidence.numbers.length ? 2 : Math.min(2, evidence.words.length);
	let hits = 0;
	for (const w of evidence.words) if (lower.includes(w) && ++hits >= need) return true;
	return false;
}

/**
 * Wire the durable work channel into a live session: context drops, ambient
 * note-viewing state, and task results — each injected via the session's
 * text path when the session is live, with the tuned guard markers and
 * fallback semantics preserved exactly.
 */
export function wireDurableChannels(session: VoiceSession, opts: DurableChannelOptions): { enqueue: (item: ResultItem) => void } {
	const { cartesiaApiKey = '', generateSpeech = null } = opts;

	// Watch for context drops (keyboard shortcut)
	// task-bridge always writes to tasks/ for sutando-core; also inject into Gemini if active
	startContextDropWatcher((content) => {
		if (session.sessionManager.isActive && session.clientConnected) {
			console.log(`${ts()} [ContextDrop] Injecting into Gemini conversation`);
			injectText(session, frameContextDrop(content));
		}
	});

	// Ambient UI state: when the user opens a note in the web client, inject
	// its content so Gemini can answer questions about it without being told
	// the path. Silent acknowledgement — unlike context drop this is not an
	// action, just situational awareness.
	startNoteViewingWatcher((slug, content) => {
		if (session.sessionManager.isActive && session.clientConnected) {
			// If the note body contains words that match the GOODBYE RULE
			// trigger list in system instructions, inject METADATA ONLY —
			// NOT the body. Guard-marker wrappers are not strong enough:
			// observed 2026-04-09 at 23:43, notes/uiuc-trip-conflicts.md
			// contains "better to fully disconnect", was injected with
			// <NOTE_START>/<NOTE_END> guards and an explicit "do not match
			// against GOODBYE RULE" preamble, and Gemini matched the
			// trigger anyway and fired end_session 7 seconds into the
			// session. System instructions outweigh turn-level guards.
			//
			// Metadata-only fallback: Gemini knows WHAT the user is
			// viewing but not the content. If it needs content to answer
			// a question, it can call read_note(slug) directly — that's
			// an explicit tool path and Gemini is less likely to
			// hallucinate triggers from it.
			const GOODBYE_TRIGGERS = /\b(goodbye|bye|disconnect|see you later|end[\s_]session)\b/i;
			const hasTrigger = GOODBYE_TRIGGERS.test(content);
			const truncated = content.length > 4000 ? content.slice(0, 4000) + '\n\n[...truncated]' : content;
			if (hasTrigger) {
				console.log(`${ts()} [NoteView] Injecting METADATA ONLY for ${slug} (content contains GOODBYE RULE trigger words)`);
				injectText(session, frameNoteViewMetadata(slug));
			} else {
				console.log(`${ts()} [NoteView] Injecting: ${slug}`);
				injectText(session, frameNoteViewFull(slug, truncated));
			}
			return true;  // handled — watcher bumps its debounce
		}
		// Not connected: return false so the watcher keeps the event
		// pending. On reconnect we reset the debounce (below) and this
		// poll will fire again with the same content.
		return false;
	});

	// A result answers its own task and every task the core deduped into it that the user has not heard.
	const answering = (taskId?: string): Pick<ResultItem, 'requests' | 'alsoFor'> => {
		if (!taskId) return {};
		const others = voiceTaskStore.answeredBy(taskId).filter(([, row]) => row.delivery !== 'spoken' && row.delivery !== 'injected');
		const own = voiceTaskStore.get(taskId)?.text;
		return {
			requests: [own, ...others.map(([, row]) => row.text)].filter((r): r is string => !!r),
			alsoFor: others.map(([id]) => id),
		};
	};

	// A result no work call can carry (its call was cancelled, its connection replaced, or the agent
	// restarted): a system notice when the session can speak, held through a meeting; else one DM copy.
	const heldForMeeting: ResultItem[] = [];
	// System notices can stop reaching the model after a fresh dial (bodhi 0.4.6's notification actor waits for a
	// turn that never comes). From that boundary until the model's next completed spoken turn, a result goes to
	// the model as live input instead.
	let noticesSuspect = false;
	// One result at a time, each confirmed by the model's next turn (spoken) or handed over again once.
	const pacer = createHandoffPacer({
		markSpoken: (spoken) => { for (const id of spoken) voiceTaskStore.set(id, 'spoken'); },
		confirmMs: opts.handoffConfirmMs,
		log: (m) => console.log(`${ts()} ${m}`),
	});
	pacerHeld = () => pacer.held();
	const interruptedTurns = new Set<string>();
	// Results returned through open calls: a spoken turn counts as the result only when its words carry the result's
	// content; unsaid after returnNudgeMs it is nudged, after returnReleaseMs (or a reconnect) handed over again.
	type Returned = { ids: string[]; at: number; evidence: ReturnType<typeof resultEvidence>; request: string; nudged: boolean; nudge: ReturnType<typeof setTimeout>; release: ReturnType<typeof setTimeout> };
	const returnedUnheard: Returned[] = [];
	const dropReturned = (r: Returned) => { clearTimeout(r.nudge); clearTimeout(r.release); const i = returnedUnheard.indexOf(r); if (i >= 0) returnedUnheard.splice(i, 1); };
	const turnWords = (): string | null => {
		const items = (session as unknown as { conversationContext?: { items?: ReadonlyArray<{ role?: string; content?: string | null }> } }).conversationContext?.items;
		if (!items) return null;
		for (let i = items.length - 1; i >= 0; i--) if (items[i].role === 'assistant') return items[i].content ?? '';
		return '';
	};
	let modelTurnStartedAt = 0;
	let modelSpokeInTurn = false;
	session.eventBus.subscribe('turn.start', () => { modelTurnStartedAt = Date.now(); modelSpokeInTurn = false; });
	// A fresh dial abandons the old connection's calls and whatever the model held unsaid: a result returned to a
	// call, or handed to the pacer, is handed over again. A resume keeps the calls and the context, so RECONNECTING
	// alone changes nothing here.
	session.eventBus.subscribe('session.reconnectBoundary', () => {
		if (!noticesSuspect && opts.injectLive) console.log(`${ts()} [RelayAgent] connection replaced: results with no open call go as live input until the model speaks`);
		noticesSuspect = true;
		for (const r of returnedUnheard.splice(0)) { clearTimeout(r.nudge); clearTimeout(r.release); for (const id of r.ids) voiceTaskStore.unsetHandedOver(id); }
		for (const id of pacer.held()) voiceTaskStore.unsetHandedOver(id);
	});
	session.eventBus.subscribe('response.first_audio', () => { modelSpokeInTurn = true; });
	session.eventBus.subscribe('turn.end', () => {
		if (!modelSpokeInTurn) return;
		modelSpokeInTurn = false;
		// The model completed a spoken turn on the new connection: its notifications flow again.
		if (noticesSuspect && (session.sessionManager as { state?: string }).state !== 'RECONNECTING') noticesSuspect = false;
		const words = turnWords();
		for (const r of [...returnedUnheard]) {
			if (r.at > modelTurnStartedAt) continue;
			// Nothing distinctive to look for (the result only restates the request): the turn after the nudge counts.
			const noEvidence = !r.evidence.numbers.length && !r.evidence.words.length;
			if (words !== null && (noEvidence ? !r.nudged : !turnSaysResult(words, r.evidence))) {
				console.log(`${ts()} [RelayAgent] ${r.ids[0]}: the model spoke, but not the result; still waiting for it`);
				continue;
			}
			for (const id of r.ids) voiceTaskStore.set(id, 'spoken');
			dropReturned(r);
		}
	});
	session.eventBus.subscribe('turn.start', () => pacer.onTurnStart());
	session.eventBus.subscribe('response.first_audio', () => pacer.onModelAudio());
	// Only the client's microphone detection counts: Gemini marks a start on every transcription chunk and no end.
	session.eventBus.subscribe('speech.user_started', (e: { source?: string }) => pacer.onUserSpeechStarted(e));
	session.eventBus.subscribe('speech.user_ended', (e: { source?: string }) => pacer.onUserSpeechEnded(e));
	session.eventBus.subscribe('turn.interrupted', (e: { turnId?: string }) => { if (e?.turnId) interruptedTurns.add(String(e.turnId)); });
	session.eventBus.subscribe('turn.end', (e: { turnId?: string }) => {
		pacer.onTurnEnd(!!e?.turnId && interruptedTurns.delete(String(e.turnId)));
	});
	let stuckSeq = 0;
	let heldTimer: ReturnType<typeof setInterval> | null = null;
	const record = (item: ResultItem, delivery: 'injected' | 'dm') => {
		for (const id of [item.taskId, ...(item.alsoFor ?? [])]) if (id) voiceTaskStore.set(id, delivery);
	};
	const holdForMeeting = (item: ResultItem) => {
			heldForMeeting.push(item);
			console.log(`${ts()} [TaskBridge] Meeting mode: holding the result until the meeting ends (${heldForMeeting.length} held)`);
			heldTimer ??= setInterval(() => {
				if (meetingHoldsModel(session)) return;
				clearInterval(heldTimer!);
				heldTimer = null;
				for (const held of heldForMeeting.splice(0)) deliverElsewhere(held);
			}, 2_000);
			heldTimer.unref?.();
	};
	const deliverElsewhere = (item: ResultItem) => {
		if (meetingHoldsModel(session)) { holdForMeeting(item); return; }
		pacer.submit(viaNotice(item));
	};
	const canSpeak = () => session.sessionManager.isActive && session.clientConnected;
	const notice = (item: ResultItem) => canSpeak() && session.tryPublishSystemNotification(frameResult(item));
	const ids = (item: ResultItem) => (item.taskId ? [item.taskId, ...(item.alsoFor ?? [])] : []);
	const moot = (item: ResultItem) => (item.about ? !notPickedStillTrue(item.about) : resultIsMoot(voiceTaskStore, ids(item)));
	// A result handed to the model as a system notice; it waits for a reconnect, then goes to the DM.
	// Which tasks a result answers is read when it is handed: the core may mark one deduped into it after it landed.
	const current = (item: ResultItem): ResultItem => (item.taskId ? { ...item, ...answering(item.taskId) } : item);
	const viaNotice = (item: ResultItem): Handoff => ({
		ids: ids(item), label: item.taskId ?? 'result', moot: () => moot(item),
		hand: () => {
			// A meeting that began while this waited its turn: not said into it (bodhi's notifications do not check).
			if (meetingHoldsModel(session)) return false;
			const now = current(item);
			// A notice already went unheard for this task, or none has been heard since the reconnect: live input.
			const unheardBefore = ids(now).some((id) => (voiceTaskStore.get(id)?.replays ?? 0) > 0);
			if (opts.injectLive && (noticesSuspect || unheardBefore)) {
				if (!canSpeak()) return false;
				console.log(`${ts()} [RelayAgent] ${item.taskId ?? 'result'}: handed as live input (${noticesSuspect ? 'after a reconnect' : 'a notice went unheard'})`);
				// Recorded handed over only once the text has gone in: one held behind the model's speech may be a while.
				let recorded = false;
				const mark = () => { if (!recorded) { recorded = true; record(now, 'injected'); } };
				if (opts.injectLive(frameResult(now), mark) !== false) mark();
				return true;
			} else if (!notice(now)) return false;
			record(now, 'injected'); return true;
		},
		fallback: () => (meetingHoldsModel(session) ? holdForMeeting(item) : retryThenDm(item)),
	});
	const retryThenDm = (item: ResultItem) => {
		deliverWithRetry({
			attempt: () => {
				if (meetingHoldsModel(session)) { holdForMeeting(item); return true; }
				if (!canSpeak()) return false;
				pacer.submit({ ...viaNotice(item), fallback: () => (meetingHoldsModel(session) ? holdForMeeting(item) : toDm(item)) });
				return true;
			},
			// A reconnect takes seconds; wait up to a minute for it before the DM.
			delaysMs: [0, ...(opts.notReadyRetriesMs ?? Array(30).fill(2_000))],
			onExhausted: () => toDm(item),
		});
	};
	const toDm = (item: ResultItem) => {
		const taskId = item.taskId;
		// A voice task's result: one DM copy, which the drain speaks on reconnect if no bridge takes it.
		if (taskId?.startsWith('task-')) {
			if (voiceTaskStore.get(taskId)?.delivery === 'dm') return;
			void forwardOfflineVoiceResult(taskId, item.text)
				.then(() => record(item, 'dm'))
				.catch((e) => console.error(`${ts()} [TaskBridge] offline forward failed for ${taskId}:`, e));
			return;
		}
		// Stuck-voice fallback (Susan's PR #924 review, Q3): always also a DM, so a result is never
		// silently lost; Cartesia stays a bonus path for a user watching the web client.
		console.log(`${ts()} [TaskBridge] Voice not active after waiting — falling back to Discord DM${cartesiaApiKey && generateSpeech ? ' + Cartesia' : ''}`);
		try {
			// One file per result: two can fall back in the same millisecond.
			publishResultFile(`proactive-voice-stuck-${Date.now()}-${++stuckSeq}.txt`, `🎤 Voice session was stuck — couldn't speak this. Task result:\n\n${item.text}`);
		} catch (e) {
			console.error(`${ts()} [TaskBridge] Failed to write stuck-voice Discord fallback:`, e);
		}
		if (cartesiaApiKey && generateSpeech) {
			const truncated = (item.text.match(/^[\s\S]{0,500}[.!?]/)?.[0] || item.text.slice(0, 500)).trim();
			generateSpeech(truncated, { category: 'result', label: 'task-result' }).then(audioPath => {
				const relativeSrc = audioPath.startsWith(WORKSPACE_DIR)
					? audioPath.slice(WORKSPACE_DIR.replace(/\/$/, '').length + 1)
					: audioPath;
				writeFileSync(statusPath('dynamic-content.json', WORKSPACE_DIR), JSON.stringify({
					type: 'audio', src: relativeSrc, title: 'Task Complete',
				}));
				console.log(`${ts()} [CartesiaTTS] Audio generated: ${audioPath}`);
			}).catch(err => console.error(`${ts()} [CartesiaTTS] ${err.message}`));
		}
	};
	const deliver = (item: ResultItem) => {
		// Corrected, or already said: end its call, say nothing.
		if (moot(item)) {
			for (const id of ids(item)) opts.relay.endCall(id, 'delivered_separately');
			console.log(`${ts()} [RelayAgent] ${item.taskId ?? 'result'}: already heard or corrected; not said`);
			return;
		}
		if (item.taskId && opts.relay.isWaiting(item.taskId)) {
			// The open call takes the result at once, not through the pacer: WHEN_IDLE lets Gemini pick the
			// moment. A meeting holds the model: the call ends and the result waits for the meeting's end.
			if (meetingHoldsModel(session)) {
				for (const id of ids(item)) opts.relay.endCall(id, 'delivered_separately');
				holdForMeeting(item);
				return;
			}
			// A result handed to a session nobody hears would be recorded heard: it goes the offline way.
			const now = current(item);
			if (canSpeak() && opts.relay.offerResult(now)) {
				const request = (now.requests?.[0] ?? voiceTaskStore.get(item.taskId)?.text ?? item.taskId).replace(/"/g, "'").slice(0, 120);
				const r: Returned = {
					ids: ids(now), at: Date.now(), evidence: resultEvidence(now.text, [request, ...(now.requests ?? [])].join(' ')), request, nudged: false,
					nudge: setTimeout(() => {
						if (!returnedUnheard.includes(r) || !canSpeak()) return;
						r.nudged = true;
						const text = framedSystem(`The result for the request "${request}" came back through its work call but you have not told the user yet. Tell them now, in one or two sentences.`);
						console.log(`${ts()} [RelayAgent] ${r.ids[0]}: returned ${Math.round((Date.now() - r.at) / 1000)} s ago and not said; nudging`);
						if (noticesSuspect && opts.injectLive) opts.injectLive(text); else session.tryPublishSystemNotification(text);
					}, opts.returnNudgeMs ?? RETURN_NUDGE_MS),
					release: setTimeout(() => {
						if (!returnedUnheard.includes(r)) return;
						console.log(`${ts()} [RelayAgent] ${r.ids[0]}: still not said after the nudge; handing it over again`);
						dropReturned(r);
						for (const id of r.ids) voiceTaskStore.unsetHandedOver(id);
					}, opts.returnReleaseMs ?? RETURN_RELEASE_MS),
				};
				r.nudge.unref?.(); r.release.unref?.();
				returnedUnheard.push(r);
				console.log(`${ts()} [RelayAgent] ${item.taskId}: result returned to its work call`);
				return;
			}
			deliverElsewhere(item);
			return;
		}
		console.log(`${ts()} [TaskBridge] Delivering result to the user${item.note ? ' (with a delivery note)' : ''}`);
		deliverElsewhere(item);
	};

	startResultWatcher((result, deliveryNote, meta) => {
		// A message with no task of its own (a summary) never stands in for a voice task's own result: numbers
		// in common prove the two mention the same thing, not that one answers the other (review of #5348, B3).
		deliver({ text: result, note: deliveryNote, taskId: meta?.taskId, ...answering(meta?.taskId) });
	}, () => session.clientConnected);

	// The relay agent's reconcile loop: whatever the core finished that the user has not heard
	// (sent to the DM, its call gone, the agent restarted) is handed over again once the session can speak.
	const reconcile = setInterval(() => {
		if (!session.sessionManager.isActive || !session.clientConnected || meetingHoldsModel(session)) return;
		try {
			// A task's own waiting call goes first: a task answered in another's result still has one.
			reconcileVoiceTasks(
				(text, note, meta) => deliver({ text, note, taskId: meta.taskId, framed: meta.framed, about: meta.about, ...answering(meta.taskId) }),
				// Queued in the pacer: on its way, not lost.
				(id) => pacer.has(id),
			);
		} catch (err) {
			console.error(`${ts()} [RelayAgent] reconcile failed (will retry):`, err);
		}
	}, opts.reconcileMs ?? 5_000);
	reconcile.unref?.();
	return { enqueue: deliverElsewhere };
}

// ── Session observability recorder (step 5a-3) ───────────────────────────────
// Moved from voice-agent main(): the per-session metrics state (events, tool
// calls, transcript), the idempotent flush into conversation-store, and the
// realtime usage ticker. Adapter callbacks push into the exposed arrays —
// the same mutation pattern the inline code used, so callback bodies change
// only in the variable prefix. Phone (5b-2) gets the same recorder with
// source: 'phone'.

import { recordSession } from './conversation-store.js';
import { startVoiceTicker, type TickerControl } from './observability/realtime.js';

export interface SessionRecorder {
	events: Array<{ event: string; timestamp: string }>;
	toolCalls: Array<{ name: string; durationMs: number; timestamp: string }>;
	transcript: Array<{ role: string; text: string }>;
	/** Reset per-logical-session state (fresh arrays + start time). */
	reset(): void;
	/** Flush metrics once (idempotent) and stop the usage ticker FIRST — a
	 * leaked ticker must never fire past a flush. `extra` merges surface-specific
	 * columns into the recordSession payload (phone carries callSid, caller,
	 * isOwner, isMeeting, pendingTasks and overrides transcriptLines/durationMs/
	 * sessionId — see conversation-server finalize). Voice calls flush() with no
	 * args, so its payload is unchanged. */
	flush(extra?: Record<string, unknown>): void;
	/** Start (or restart) the realtime usage ticker for a fresh logical
	 * session. Stops any lingering ticker first. */
	startTicker(model: string): void;
	/** True once flush() has recorded this logical session — the reconnect
	 * path uses it to decide whether a fresh reset is needed. */
	readonly wasFlushed: boolean;
}

/**
 * Options for {@link createSessionRecorder}.
 * `tickerFactory` lets a surface inject its own usage ticker so the recorder
 * stays surface-agnostic: voice defaults to `startVoiceTicker` (kind
 * `voice.session`), phone injects `startPhoneTicker` (kind `phone.call`, carries
 * callSid/isOwner/isMeeting). The recorder owns the ticker's lifecycle either
 * way (start on startTicker, stop-before-guard on flush).
 */
export interface SessionRecorderOptions {
	tickerFactory?: (model: string) => TickerControl;
}

export function createSessionRecorder(
	source: string,
	sessionId: string,
	opts: SessionRecorderOptions = {},
): SessionRecorder {
	const events: Array<{ event: string; timestamp: string }> = [];
	const toolCalls: Array<{ name: string; durationMs: number; timestamp: string }> = [];
	const transcript: Array<{ role: string; text: string }> = [];
	let sessionStart = Date.now();
	let metricsWritten = false;
	let ticker: TickerControl | null = null;
	// Default (voice) ticker: verbatim to the pre-extraction startVoiceTicker
	// call. Phone overrides this with a startPhoneTicker factory.
	const makeTicker = opts.tickerFactory ?? ((model: string) => startVoiceTicker({
		sessionId,
		model,
		toolCallsGetter: () => toolCalls.length,
	}));

	return {
		events, toolCalls, transcript,
		get wasFlushed() { return metricsWritten; },
		reset() {
			events.length = 0; toolCalls.length = 0; transcript.length = 0;
			sessionStart = Date.now();
			metricsWritten = false;
		},
		flush(extra?: Record<string, unknown>) {
			// Spine usage: flush the final partial bucket and clear the interval FIRST,
			// before the metricsWritten guard. stop() is idempotent (ticker→null),
			// so a double-flush never double-emits — but doing it before the guard means
			// a leaked ticker can NEVER keep firing past a flush (otherwise it would emit
			// phantom voice.seconds every USAGE_TICK_MS during the post-session idle gap).
			try { ticker?.stop(); ticker = null; } catch {}
			if (metricsWritten) return;
			metricsWritten = true;
			try {
				// Surface-specific columns (extra) win over the recorder's defaults:
				// phone overrides sessionId (→null; it keys rows by callSid), plus
				// transcriptLines/durationMs (its transcript + startTime live on the
				// CallSession, not this recorder). Voice passes no extra → unchanged.
				recordSession({
					source,
					sessionId,
					durationMs: Date.now() - sessionStart,
					transcriptLines: transcript.length,
					toolCount: toolCalls.length,
					toolCalls,
					events,
					...extra,
				});
				const loggedTranscriptLines = (extra?.transcriptLines as number | undefined) ?? transcript.length;
				console.log(`${ts()} [Observability] Recorded ${source} session: ${toolCalls.length} tools, ${events.length} events, ${loggedTranscriptLines} transcript lines (sqlite, #603)`);
			} catch (err) {
				console.log(`${ts()} [Observability] Failed to write metrics: ${err}`);
			}
		},
		startTicker(model: string) {
			ticker?.stop();
			ticker = makeTicker(model);
		},
	};
}
