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

import { writeFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import type { VoiceSession } from 'bodhi-realtime-agent';
import { resolveWorkspace, statusPath } from './workspace_default.js';
import { injectText } from './browser-tools.js';
import { frameContextDrop, frameNoteViewMetadata, frameNoteViewFull, frameTaskResult } from './inject-framing.js';
import { deliverWithRetry } from './inject-delivery.js';
import { startResultWatcher, startContextDropWatcher, startNoteViewingWatcher } from './task-bridge.js';

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
}

/**
 * Wire the durable work channel into a live session: context drops, ambient
 * note-viewing state, and task results — each injected via the session's
 * text path when the session is live, with the tuned guard markers and
 * fallback semantics preserved exactly.
 */
export function wireDurableChannels(session: VoiceSession, opts: DurableChannelOptions = {}): void {
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

	startResultWatcher((result) => {
		console.log(`${ts()} [TaskBridge] Delivering result to user`);
		// Re-check session state inside the timer rather than at callback
		// time. Reason: TaskBridge delivers `voice-*.txt` results the
		// instant the WebSocket reconnects, but Gemini setup completes
		// ~100ms after that. Without this delay-then-check pattern, a
		// voice-only push that lands during the connect-but-not-active
		// window would silently fall through to the Cartesia branch
		// (which is usually disabled). 2026-05-20 02:36:44 incident:
		// voice-test-1779244500.txt was "delivered" per the bridge log
		// but never spoken because isActive was false at callback time
		// (Gemini setup completed 106ms later). Now the check fires at
		// T+1500ms when setup is reliably finished.
		const inject = () => {
			if (session.sessionManager.isActive && session.clientConnected) {
				injectText(session, frameTaskResult(result));
				return true;
			}
			return false;
		};
		// First attempt after 1.5s (matches prior behavior). If still not
		// active, do one retry at 3s. After that, fall through to Cartesia
		// — no infinite retry, since a stuck session shouldn't pin the
		// result forever.
		deliverWithRetry({
			attempt: inject,
			onExhausted: () => {
				// Stuck-voice fallback. Per Susan's PR #924 review (Q3): Cartesia
				// only reaches the user if they're watching the web client with
				// audio playback — a user in a stuck voice session is probably
				// looking at the voice surface, not the web UI. So the
				// stuck-voice result can go into the void. Always also write a
				// Discord DM via a proactive-*.txt file so the result is never
				// silently lost. Cartesia stays as a bonus path when available
				// (some users keep the web UI open).
				console.log(`${ts()} [TaskBridge] Voice not active after 3s — falling back to Discord DM${cartesiaApiKey && generateSpeech ? ' + Cartesia' : ''}`);
				try {
					const proactiveTs = Math.floor(Date.now() / 1000);
					const proactivePath = join(WORKSPACE_DIR, 'results', `proactive-voice-stuck-${proactiveTs}.txt`);
					const dmBody = `🎤 Voice session was stuck — couldn't speak this. Task result:\n\n${result}`;
					// Publish atomically: a consumer must never observe a partial body.
					const proactiveTmp = `${proactivePath}.tmp-${process.pid}`;
					writeFileSync(proactiveTmp, dmBody);
					renameSync(proactiveTmp, proactivePath);
				} catch (e) {
					console.error(`${ts()} [TaskBridge] Failed to write stuck-voice Discord fallback:`, e);
				}
				if (cartesiaApiKey && generateSpeech) {
					const truncated = (result.match(/^[\s\S]{0,500}[.!?]/)?.[0] || result.slice(0, 500)).trim();
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
			},
		});
	}, () => session.clientConnected);
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
