/**
 * Meeting mode on bodhi's dictation (transcription) mode: while a meeting runs
 * the voice model is quiesced, each final transcript line is appended to the
 * day's meeting note, and an exit phrase returns the session to agent mode.
 */

import { appendFileSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { STTProvider } from 'bodhi-realtime-agent';

const EXIT_PATTERN =
	/\b(?:(?:end|stop|exit|finish)\s+(?:the\s+)?(?:dictation|transcription|meeting(?:\s+mode)?|note[- ]?taking)|done\s+dictating|meeting\s+is\s+over|active\s+mode|sutando,?\s+come\s+back)\b/i;

export function isMeetingExitPhrase(text: string): boolean {
	return EXIT_PATTERN.test(text);
}

function hhmmss(d: Date): string {
	return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function ensureMeetingNote(notePath: string, today: string): void {
	if (existsSync(notePath)) return;
	mkdirSync(dirname(notePath), { recursive: true });
	writeFileSync(notePath, `---\ntitle: Meeting notes — ${today}\ndate: ${today}\ntags: [meeting, notes]\n---\n\n`);
}

export function appendTranscriptLine(notePath: string, text: string, at: Date = new Date()): void {
	appendFileSync(notePath, `- [${hhmmss(at)}] ${text}\n`);
}

export function appendTranscriptHeader(notePath: string, at: Date = new Date()): void {
	appendFileSync(notePath, `\n## Transcript (from ${hhmmss(at)})\n`);
}

/**
 * Defers entering dictation until the turn after the one carrying switch_mode
 * (the spoken confirmation) completes, with a fallback for models that never send one.
 */
export function createMeetingEntryGate(opts: { fallbackMs: number; onFire: () => void }) {
	let pending: { turns: number; timer: ReturnType<typeof setTimeout> } | null = null;
	const cancel = () => {
		if (pending) clearTimeout(pending.timer);
		pending = null;
	};
	const fire = () => {
		if (!pending) return;
		cancel();
		opts.onFire();
	};
	return {
		schedule() {
			cancel();
			pending = { turns: 0, timer: setTimeout(fire, opts.fallbackMs) };
		},
		cancel,
		noteTurnCompleted() {
			if (pending && ++pending.turns >= 2) fire();
		},
		get pending() {
			return pending !== null;
		},
	};
}

export interface MeetingDictationSession {
	setTranscriptionMode(mode: 'agent' | 'transcription'): Promise<void>;
	getTranscriptionMode(): 'agent' | 'transcription';
	clearDictationBuffer(): void;
	injectText(text: string, opts: { mode: 'live' | 'quiet' }): Promise<boolean>;
}

export interface MeetingDictationDeps {
	session: MeetingDictationSession;
	provider: STTProvider;
	/** Resolved per meeting so a meeting crossing midnight keeps one file. */
	notePathFor: (today: string) => string;
	/** Called after an exit phrase returned the session to agent mode. */
	onExitByVoice: () => void;
	log: (msg: string) => void;
	now?: () => Date;
}

/**
 * Wraps the provider's transcript callback (which bodhi points at its
 * dictation buffer) so every final line also lands in the note file.
 */
export function attachMeetingDictation(deps: MeetingDictationDeps) {
	const now = deps.now ?? (() => new Date());
	let notePath: string | null = null;
	let lines = 0;
	let exiting = false;
	const bufferSink = deps.provider.onTranscript;

	deps.provider.onTranscript = (text, turnId) => {
		if (!text) return;
		const exitAt = text.search(EXIT_PATTERN);
		if (exitAt >= 0) {
			deps.log(`[MeetingDictation] exit phrase heard: "${text}"`);
			const before = text.slice(0, exitAt).replace(/[\s,，]+$/, '');
			if (before) record(before, turnId);
			void exit({ byVoice: true });
			return;
		}
		record(text, turnId);
	};

	function record(text: string, turnId: number | undefined): void {
		bufferSink?.(text, turnId);
		if (notePath) {
			try {
				appendTranscriptLine(notePath, text, now());
				lines++;
			} catch (err) {
				deps.log(`[MeetingDictation] append failed: ${(err as Error).message}`);
			}
		}
	}

	async function enter(): Promise<void> {
		if (deps.session.getTranscriptionMode() === 'transcription') return;
		const at = now();
		const today = at.toISOString().slice(0, 10);
		notePath = deps.notePathFor(today);
		lines = 0;
		ensureMeetingNote(notePath, today);
		appendTranscriptHeader(notePath, at);
		deps.session.clearDictationBuffer();
		await deps.session.setTranscriptionMode('transcription');
		deps.log(`[MeetingDictation] transcribing to ${notePath}`);
	}

	async function exit(opts: { byVoice: boolean }): Promise<void> {
		if (exiting || deps.session.getTranscriptionMode() !== 'transcription') return;
		exiting = true;
		try {
			await deps.session.setTranscriptionMode('agent');
			const path = notePath;
			const count = lines;
			notePath = null;
			deps.session.clearDictationBuffer();
			deps.log(`[MeetingDictation] back to agent mode (${count} lines in ${path})`);
			if (opts.byVoice) deps.onExitByVoice();
			if (path) {
				await deps.session.injectText(
					`[Meeting ended. The transcript (${count} lines) is saved in ${path}. Tell the user in one short sentence that the notes are saved.]`,
					{ mode: 'live' },
				);
			}
		} finally {
			exiting = false;
		}
	}

	return {
		enter,
		exit: () => exit({ byVoice: false }),
		get notePath() {
			return notePath;
		},
	};
}
