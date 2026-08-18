/**
 * ACTIVE-silence recovery coordinator (Phase 1 armed mode) — the impure driver
 * around the pure reducer in voice-active-silence-watchdog.ts: executes
 * effects against the bodhi session surface (recoverUpstream, client JSON),
 * owns retry timers, the terminal voice-stalled push/resend, the retry-ack
 * wire, and the reducer↔transport attempt-epoch correlation.
 */

import {
	type MatrixFactsLike,
	type RecoveryEffect,
	type RecoveryEvent,
	type RecoveryState,
	initialRecoveryState,
	reduceRecovery,
} from './voice-active-silence-watchdog.js';

/** Structural bodhi surface — no bodhi import, so this compiles against any
 *  pin; recoverySurfaceSupported() is the runtime gate. */
export interface RecoverySessionSurface {
	recoverUpstream(args: {
		reason: 'active-silence' | 'human-retry' | 'fatal-backoff-clear';
		skipContextInjection: boolean;
		holdSyntheticUntilFreshSpeech: boolean;
	}): { attemptEpoch: number; activated: Promise<void>; incumbentClosed: Promise<'closed' | 'forced'> };
	sendJsonToClient(message: Record<string, unknown>): void;
	getRecoveryCapabilities(): {
		version: number;
		recoverUpstream: boolean;
		transportGenerations: boolean;
	};
}

/** Armed mode requires the full bodhi recovery surface; anything less falls
 *  back to shadow (the capability-validation rule from the design doc). */
export function recoverySurfaceSupported(session: unknown): boolean {
	const s = session as Partial<RecoverySessionSurface>;
	if (typeof s.recoverUpstream !== 'function') return false;
	if (typeof s.sendJsonToClient !== 'function') return false;
	if (typeof s.getRecoveryCapabilities !== 'function') return false;
	try {
		const caps = s.getRecoveryCapabilities();
		return caps.recoverUpstream === true && caps.transportGenerations === true;
	} catch {
		return false;
	}
}

export interface RetryUpstreamCommand {
	type: 'voice.retryUpstream';
	version: 1;
	voiceSessionId: string;
	clientEpoch: number;
	stalledAttemptEpoch: number;
	requestId: string;
}

const RETRY_KEYS = [
	'type',
	'version',
	'voiceSessionId',
	'clientEpoch',
	'stalledAttemptEpoch',
	'requestId',
] as const;

const isInt = (v: unknown, min: number): v is number =>
	typeof v === 'number' && Number.isInteger(v) && v >= min;

/** Schema-faithful voice.retryUpstream.v1 (additionalProperties: false). */
export function parseRetryUpstreamCommand(msg: unknown): RetryUpstreamCommand | null {
	if (typeof msg !== 'object' || msg === null || Array.isArray(msg)) return null;
	const m = msg as Record<string, unknown>;
	if (Object.keys(m).length !== RETRY_KEYS.length) return null;
	if (!RETRY_KEYS.every((k) => k in m)) return null;
	if (m.type !== 'voice.retryUpstream' || m.version !== 1) return null;
	if (typeof m.voiceSessionId !== 'string' || m.voiceSessionId.length < 1) return null;
	if (!isInt(m.clientEpoch, 0)) return null;
	if (!isInt(m.stalledAttemptEpoch, 1)) return null;
	// JSON Schema length counts characters (code points), not UTF-16 units.
	if (typeof m.requestId !== 'string') return null;
	const requestIdChars = Array.from(m.requestId).length;
	if (requestIdChars < 1 || requestIdChars > 128) return null;
	return m as unknown as RetryUpstreamCommand;
}

export interface CoordinatorTickInput {
	at: number;
	sessionState: string;
	facts: MatrixFactsLike;
	/** Newest above-floor speech time from the audio-health ledger (null: none). */
	lastAboveFloorAt: number | null;
	pendingToolCount: number;
	/** Client-delivered audio advanced this window (functional-recovery clear). */
	deliveredAdvanced: boolean;
}

export interface CoordinatorOptions {
	voiceSessionId: string;
	session: RecoverySessionSurface;
	requiredTicks: number;
	log?: (msg: string) => void;
	/** Ledger sink for effect rows; rows are primitive-only and carry the
	 *  ledger's `row` discriminator. */
	record?: (row: { row: string } & Record<string, unknown>) => void;
	/** Reducer clock domain (one process-local domain for every event). */
	nowFn?: () => number;
	/** Wall clock for wire frames (enteredAtUnixMs). */
	wallNowFn?: () => number;
	setTimer?: (fn: () => void, ms: number) => unknown;
	clearTimer?: (handle: unknown) => void;
}

const ACK_CACHE_CAP = 64;

export class VoiceSilenceRecoveryCoordinator {
	private st: RecoveryState = initialRecoveryState();
	private readonly opts: CoordinatorOptions;
	private readonly now: () => number;
	private readonly wallNow: () => number;
	private readonly setTimer: (fn: () => void, ms: number) => unknown;
	private readonly clearTimer: (handle: unknown) => void;
	private clientEpochCounter = 0;
	private lastSpeechAt: number | null = null;
	private retryTimer: unknown = null;
	private meeting = false;
	private stalledEnteredAtUnixMs: number | null = null;
	/** reducer attemptEpoch -> bodhi dial attemptEpoch, for setup-ok correlation. */
	private readonly bodhiAttemptByReducerEpoch = new Map<number, number>();
	/** Pending non-background tool calls (veto input). */
	private readonly pendingTools = new Set<string>();
	/** requestId -> ack frame; duplicates re-send the original ack verbatim. */
	private readonly ackByRequestId = new Map<string, Record<string, unknown>>();

	constructor(opts: CoordinatorOptions) {
		this.opts = opts;
		this.now = opts.nowFn ?? Date.now;
		this.wallNow = opts.wallNowFn ?? Date.now;
		this.setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
		this.clearTimer = opts.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
	}

	/** Terminal latch — voice-agent gates the ordinary CLOSED guard on this. */
	get isTerminal(): boolean {
		return this.st.phase === 'terminal';
	}

	get phase(): RecoveryState['phase'] {
		return this.st.phase;
	}

	/** Read-only state snapshot for tests and diagnostics. */
	get state(): Readonly<RecoveryState> {
		return this.st;
	}

	get pendingToolCount(): number {
		return this.pendingTools.size;
	}

	stop(): void {
		if (this.retryTimer !== null) {
			this.clearTimer(this.retryTimer);
			this.retryTimer = null;
		}
	}

	private log(msg: string): void {
		this.opts.log?.(msg);
	}

	private record(row: { row: string } & Record<string, unknown>): void {
		this.opts.record?.(row);
	}

	private dispatch(ev: RecoveryEvent): RecoveryEffect {
		const { state, effect } = reduceRecovery(this.st, ev);
		const prevPhase = this.st.phase;
		this.st = state;
		if (effect !== 'none' || state.phase !== prevPhase) {
			this.record({
				row: 'coordinator-effect',
				event: ev.kind,
				effect,
				phase: state.phase,
				attemptEpoch: state.attemptEpoch,
				episodeAttempts: state.episodeAttempts,
				transportEpoch: state.currentTransportEpoch,
				clientEpoch: state.currentClientEpoch,
				at: this.now(),
			});
		}
		if (state.phase === 'terminal' && prevPhase !== 'terminal') {
			this.stalledEnteredAtUnixMs = this.wallNow();
		}
		switch (effect) {
			case 'restart':
				this.executeRestart(ev.kind === 'retry' ? 'human-retry' : 'active-silence');
				break;
			case 'schedule-retry':
				this.armRetryTimer();
				break;
			case 'notify-stalled':
				this.pushStalled();
				break;
			default:
				break;
		}
		return effect;
	}

	private executeRestart(reason: 'active-silence' | 'human-retry'): void {
		const reducerEpoch = this.st.attemptEpoch;
		let result: ReturnType<RecoverySessionSurface['recoverUpstream']>;
		try {
			result = this.opts.session.recoverUpstream({
				reason,
				skipContextInjection: true,
				holdSyntheticUntilFreshSpeech: true,
			});
		} catch (err) {
			this.log(`recoverUpstream threw synchronously: ${(err as Error)?.message ?? err}`);
			this.dispatch({ kind: 'dialFailed', attemptEpoch: reducerEpoch, at: this.now() });
			return;
		}
		this.bodhiAttemptByReducerEpoch.set(reducerEpoch, result.attemptEpoch);
		// Bound the correlation map to the live episode's attempts.
		if (this.bodhiAttemptByReducerEpoch.size > 8) {
			const oldest = this.bodhiAttemptByReducerEpoch.keys().next().value;
			if (oldest !== undefined) this.bodhiAttemptByReducerEpoch.delete(oldest);
		}
		// The stranded old generation cannot settle tools into the new one
		// (bodhi fences those); drop them from the veto count too.
		this.pendingTools.clear();
		result.activated.catch(() => {
			this.dispatch({ kind: 'dialFailed', attemptEpoch: reducerEpoch, at: this.now() });
		});
		this.log(`restart authorized (${reason}) — reducer attempt ${reducerEpoch}, dial ${result.attemptEpoch}`);
	}

	private armRetryTimer(): void {
		if (this.retryTimer !== null) this.clearTimer(this.retryTimer);
		const epoch = this.st.attemptEpoch;
		const delay = Math.max(0, (this.st.retryNotBefore ?? 0) - this.now());
		this.retryTimer = this.setTimer(() => {
			this.retryTimer = null;
			// Re-validated at delivery by the reducer (epoch, phase, clocks).
			this.dispatch({ kind: 'retryDue', attemptEpoch: epoch, at: this.now() });
		}, delay);
	}

	private stalledFrame(): Record<string, unknown> {
		return {
			type: 'voice-stalled',
			version: 1,
			voiceSessionId: this.opts.voiceSessionId,
			clientEpoch: this.st.currentClientEpoch ?? 0,
			stalledAttemptEpoch: this.st.attemptEpoch,
			episodeAttempts: 3,
			reason: 'active-silence-attempts-exhausted',
			enteredAtUnixMs: this.stalledEnteredAtUnixMs ?? this.wallNow(),
		};
	}

	private pushStalled(): void {
		if (!this.st.clientAttached) return;
		this.opts.session.sendJsonToClient(this.stalledFrame());
		this.log(`voice-stalled pushed (attempt ${this.st.attemptEpoch}, client ${this.st.currentClientEpoch})`);
	}

	// ── Live event feeds (voice-agent wiring) ──────────────────────────────

	/** Real client attach edge (bodhi onClientConnected): mints the epoch. */
	handleClientConnected(): void {
		this.clientEpochCounter += 1;
		this.dispatch({ kind: 'clientAttached', clientEpoch: this.clientEpochCounter, at: this.now() });
	}

	handleClientDisconnected(): void {
		this.dispatch({ kind: 'clientDetached', clientEpoch: this.clientEpochCounter, at: this.now() });
	}

	/** bodhi connection-lifecycle events: the transport truth for activations
	 *  and generation closes; setup-ok correlates via the dial attempt id. */
	handleLifecycleEvent(ev: {
		kind: string;
		connectAttemptId?: string;
		transportGeneration?: number;
	}): void {
		if (ev.kind === 'setup-ok' && typeof ev.transportGeneration === 'number') {
			const dialGen = Number((ev.connectAttemptId ?? '').replace(/^att_/, ''));
			let reducerEpoch: number | null = null;
			for (const [rEpoch, bEpoch] of this.bodhiAttemptByReducerEpoch) {
				if (bEpoch === dialGen) reducerEpoch = rEpoch;
			}
			this.dispatch({
				kind: 'transportActive',
				transportEpoch: ev.transportGeneration,
				attemptEpoch: reducerEpoch,
				at: this.now(),
			});
			return;
		}
		if (ev.kind === 'generation-close' && typeof ev.transportGeneration === 'number') {
			this.dispatch({
				kind: 'closedObserved',
				transportEpoch: ev.transportGeneration,
				attemptEpoch: null,
				at: this.now(),
			});
		}
	}

	/** Client protocol command hook (bodhi onClientCommand). */
	handleClientCommand(msg: Record<string, unknown>): void {
		if (msg?.type !== 'voice.retryUpstream') return;
		const cmd = parseRetryUpstreamCommand(msg);
		if (!cmd) {
			this.record({ row: 'retry-rejected', why: 'schema', at: this.now() });
			return;
		}
		const cached = this.ackByRequestId.get(cmd.requestId);
		if (cached) {
			// Design: duplicates return the original acknowledgement and never
			// redial twice.
			this.opts.session.sendJsonToClient(cached);
			return;
		}
		let disposition: 'accepted' | 'stale' | 'not-terminal';
		let acceptedAttemptEpoch: number | null = null;
		if (cmd.voiceSessionId !== this.opts.voiceSessionId) {
			disposition = 'stale';
		} else if (this.st.phase !== 'terminal') {
			disposition = 'not-terminal';
		} else {
			const effect = this.dispatch({
				kind: 'retry',
				stalledAttemptEpoch: cmd.stalledAttemptEpoch,
				clientEpoch: cmd.clientEpoch,
				requestId: cmd.requestId,
				at: this.now(),
			});
			if (effect === 'restart') {
				disposition = 'accepted';
				acceptedAttemptEpoch = this.st.attemptEpoch;
			} else if (effect === 'schedule-retry') {
				// Parked on fatal backoff: the episode restarted; the next dial
				// mints its epoch at retryDue. Accepted names the episode's epoch.
				disposition = 'accepted';
				acceptedAttemptEpoch = this.st.attemptEpoch;
			} else {
				disposition = 'stale';
			}
		}
		const ack: Record<string, unknown> = {
			type: 'voice.retryUpstream.ack',
			version: 1,
			voiceSessionId: this.opts.voiceSessionId,
			clientEpoch: cmd.clientEpoch,
			requestId: cmd.requestId,
			stalledAttemptEpoch: cmd.stalledAttemptEpoch,
			disposition,
			acceptedAttemptEpoch,
		};
		this.ackByRequestId.set(cmd.requestId, ack);
		if (this.ackByRequestId.size > ACK_CACHE_CAP) {
			const oldest = this.ackByRequestId.keys().next().value;
			if (oldest !== undefined) this.ackByRequestId.delete(oldest);
		}
		this.opts.session.sendJsonToClient(ack);
		this.record({ row: 'retry-ack', disposition, requestId: cmd.requestId, at: this.now() });
	}

	noteMeetingMode(active: boolean): void {
		if (active === this.meeting) return;
		this.meeting = active;
		this.dispatch({ kind: 'meetingModeChanged', active, at: this.now() });
	}

	noteToolCall(toolCallId: string, execution: string | undefined): void {
		if (execution === 'background') return;
		this.pendingTools.add(toolCallId);
	}

	noteToolSettled(toolCallId: string): void {
		if (!this.pendingTools.delete(toolCallId)) return;
		if (this.st.currentTransportEpoch !== null) {
			this.dispatch({
				kind: 'toolOutcome',
				at: this.now(),
				transportEpoch: this.st.currentTransportEpoch,
			});
		}
	}

	handleFatalBackoff(until: number): void {
		this.dispatch({ kind: 'fatalBackoff', until });
	}

	handleFatalBackoffCleared(): void {
		this.dispatch({ kind: 'fatalBackoffCleared', at: this.now() });
	}

	/** The 30 s health tick: speech evidence first, then delivery evidence,
	 *  then the qualification tick itself. */
	observeTick(input: CoordinatorTickInput): void {
		if (
			input.lastAboveFloorAt !== null &&
			(this.lastSpeechAt === null || input.lastAboveFloorAt > this.lastSpeechAt)
		) {
			this.lastSpeechAt = input.lastAboveFloorAt;
			this.dispatch({ kind: 'speechObserved', at: input.lastAboveFloorAt });
		}
		if (
			input.deliveredAdvanced &&
			this.st.clientAttached &&
			this.st.currentTransportEpoch !== null &&
			this.st.currentClientEpoch !== null
		) {
			this.dispatch({
				kind: 'userVisibleResponse',
				at: input.at,
				transportEpoch: this.st.currentTransportEpoch,
				clientEpoch: this.st.currentClientEpoch,
				channel: 'audio-egress',
			});
		}
		this.dispatch({
			kind: 'tick',
			at: input.at,
			state: input.sessionState,
			facts: input.facts,
			pendingToolCount: Math.max(input.pendingToolCount, this.pendingTools.size),
			requiredTicks: this.opts.requiredTicks,
		});
	}
}
