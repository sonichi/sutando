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
		reconnectBoundary: boolean;
		turnStartPublication: boolean;
		transportGenerations: boolean;
		syntheticHold: boolean;
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
		return (
			caps.version === 1 &&
			caps.recoverUpstream === true &&
			caps.reconnectBoundary === true &&
			caps.turnStartPublication === true &&
			caps.transportGenerations === true &&
			caps.syntheticHold === true
		);
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
	/** Delivered-audio counters for the functional-recovery clear. The
	 *  coordinator owns the per-epoch baseline: a fresh epoch's reset-from-
	 *  zero counters that are already positive still count as delivery. */
	delivered: {
		epoch: number | null;
		chunksEnded: number;
		egressFrames: number;
		heartbeatSeen: boolean;
	};
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
	/** Pending non-background tool calls, keyed `${transportEpoch}:${id}` so a
	 *  stale-generation completion can never settle a reused current id. */
	private readonly pendingTools = new Map<string, number | null>();
	/** requestId -> ack frame; duplicates re-send the original ack verbatim.
	 *  accepted acks are never evicted — "duplicates never redial twice" has
	 *  no expiry; the cap bounds only non-accepted entries. */
	private readonly ackByRequestId = new Map<string, Record<string, unknown>>();
	/** A human retry accepted-but-parked on fatal backoff: the ack waits for
	 *  the authorization that mints the real epoch (design: accepted names
	 *  the new attempt epoch), and the deferred dial keeps its provenance. */
	private parkedRetry: { requestId: string; clientEpoch: number; stalledAttemptEpoch: number } | null =
		null;
	private deliveredBaseline: { epoch: number | null; chunksEnded: number; egressFrames: number } | null =
		null;
	/** stop() latch: no dispatch, push or timer may run after shutdown. */
	private stopped = false;
	/** Last seen transport epoch, for the successor-generation tool prune. */
	private stPrevTransportEpoch: number | null = null;
	/** Canary telemetry: first upstream progress after the latest restart. */
	private firstProgressSeenForAttempt = true;

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

	/** True while the coordinator owns CLOSED-state recovery: any non-idle
	 *  phase, or an idle episode it recovered (origin retained). The legacy
	 *  CLOSED guard must stand down here or it bypasses the attempt budget.
	 *  A stopped coordinator owns nothing — otherwise stopping mid-episode
	 *  stands legacy redial down against something that can no longer dial. */
	get ownsRecovery(): boolean {
		if (this.stopped) return false;
		return this.st.phase !== 'idle' || this.st.origin === 'active-silence';
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
		this.stopped = true;
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
		if (this.stopped) return 'none';
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
			this.parkedRetry = null;
		}
		// Any admitted successor generation strands the previous generation's
		// tools: their results can no longer reach this session (bodhi drops
		// them), so a lingering entry must not veto recovery forever.
		if (ev.kind === 'transportActive' && state.currentTransportEpoch !== this.stPrevTransportEpoch) {
			for (const [key, epoch] of this.pendingTools) {
				if (epoch !== state.currentTransportEpoch) this.pendingTools.delete(key);
			}
		}
		this.stPrevTransportEpoch = state.currentTransportEpoch;
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
		const parked = this.parkedRetry;
		if (parked !== null) {
			// The deferred human retry is dialing now: honest provenance, and
			// the accepted ack can finally name the freshly minted epoch.
			reason = 'human-retry';
			this.parkedRetry = null;
			this.sendAck(parked, 'accepted', reducerEpoch);
		}
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
		this.firstProgressSeenForAttempt = false;
		void result.incumbentClosed.then((outcome) => {
			if (this.stopped) return;
			this.record({ row: 'incumbent-closed', outcome, attemptEpoch: reducerEpoch, at: this.now() });
		});
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

	/** Build, cache and send one ack. accepted entries are never evicted
	 *  (dedup has no expiry); the cap bounds only non-accepted entries. */
	private sendAck(
		cmd: { requestId: string; clientEpoch: number; stalledAttemptEpoch: number },
		disposition: 'accepted' | 'stale' | 'not-terminal',
		acceptedAttemptEpoch: number | null,
	): void {
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
			for (const [key, cached] of this.ackByRequestId) {
				if (cached.disposition !== 'accepted') {
					this.ackByRequestId.delete(key);
					break;
				}
			}
		}
		this.opts.session.sendJsonToClient(ack);
		this.record({ row: 'retry-ack', disposition, requestId: cmd.requestId, at: this.now() });
	}

	/** Client protocol command hook (bodhi onClientCommand). */
	handleClientCommand(msg: Record<string, unknown>): void {
		if (this.stopped) return;
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
		// A duplicate of the currently PARKED request has no ack yet — stay
		// silent; its accepted ack goes out when the deferred dial authorizes.
		if (this.parkedRetry !== null && this.parkedRetry.requestId === cmd.requestId) return;
		if (cmd.voiceSessionId !== this.opts.voiceSessionId) {
			this.sendAck(cmd, 'stale', null);
			return;
		}
		if (this.st.phase !== 'terminal') {
			this.sendAck(cmd, 'not-terminal', null);
			return;
		}
		const effect = this.dispatch({
			kind: 'retry',
			stalledAttemptEpoch: cmd.stalledAttemptEpoch,
			clientEpoch: cmd.clientEpoch,
			requestId: cmd.requestId,
			at: this.now(),
		});
		if (effect === 'restart') {
			// executeRestart already ran inside dispatch; the epoch is minted.
			this.sendAck(cmd, 'accepted', this.st.attemptEpoch);
		} else if (effect === 'schedule-retry') {
			// Parked on fatal backoff (residue 6): defer the accepted ack until
			// authorization mints the real epoch — an ack naming a stale epoch
			// would make the client hide the banner against the wrong attempt.
			this.parkedRetry = {
				requestId: cmd.requestId,
				clientEpoch: cmd.clientEpoch,
				stalledAttemptEpoch: cmd.stalledAttemptEpoch,
			};
			this.record({ row: 'retry-parked', requestId: cmd.requestId, at: this.now() });
		} else {
			this.sendAck(cmd, 'stale', null);
		}
	}

	/** Model activity (turn.start): generation-fenced by the reducer — the
	 *  event's own transport generation is used when it carries one. */
	handleModelEvent(transportGeneration?: number): void {
		const epoch = transportGeneration ?? this.st.currentTransportEpoch;
		if (epoch === null) return;
		if (!this.firstProgressSeenForAttempt && epoch === this.st.currentTransportEpoch) {
			this.firstProgressSeenForAttempt = true;
			this.record({
				row: 'first-upstream-progress',
				attemptEpoch: this.st.attemptEpoch,
				transportEpoch: epoch,
				at: this.now(),
			});
		}
		this.dispatch({ kind: 'modelEvent', at: this.now(), transportEpoch: epoch });
	}

	noteMeetingMode(active: boolean): void {
		if (active === this.meeting) return;
		this.meeting = active;
		this.dispatch({ kind: 'meetingModeChanged', active, at: this.now() });
	}

	noteToolCall(toolCallId: string, execution: string | undefined): void {
		if (execution === 'background') return;
		this.pendingTools.set(`${this.st.currentTransportEpoch}:${toolCallId}`, this.st.currentTransportEpoch);
	}

	noteToolSettled(toolCallId: string): void {
		// The settle hook carries no generation, so resolve by STORED epoch,
		// oldest entry first: completions overwhelmingly arrive in issue
		// order, and a late old-generation completion must consume ITS OWN
		// entry — never the current generation's veto — and must not advance
		// the current anchor.
		let match: { key: string; epoch: number | null } | null = null;
		for (const [key, epoch] of this.pendingTools) {
			if (!key.endsWith(`:${toolCallId}`)) continue;
			if (match === null || (epoch ?? -1) < (match.epoch ?? -1)) match = { key, epoch };
		}
		if (match === null) return;
		this.pendingTools.delete(match.key);
		if (match.epoch !== null && match.epoch === this.st.currentTransportEpoch) {
			this.dispatch({
				kind: 'toolOutcome',
				at: this.now(),
				transportEpoch: match.epoch,
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
		// Per-epoch delivered baseline. Only CLIENT-CONFIRMED playback
		// (heartbeat chunksEnded) can clear terminal — server egress is audio
		// SENT, not delivered, and its epoch is uncorrelated, so it counts
		// only as model activity (anchor advance) below. A NEW epoch's
		// reset-from-zero counters that are already positive still count — a
		// response landing before its epoch's first tick must not be lost.
		// Speech is replayed before delivery deliberately: when the 30 s
		// window cannot order them, unlatching (fullReset) is the conservative
		// side — the watchdog can only under-arm, never re-arm on stale
		// evidence.
		const d = input.delivered;
		const fresh = this.deliveredBaseline === null || this.deliveredBaseline.epoch !== d.epoch;
		const playbackAdvanced = d.heartbeatSeen
			? fresh
				? d.chunksEnded > 0
				: d.chunksEnded > (this.deliveredBaseline as { chunksEnded: number }).chunksEnded
			: false;
		const egressAdvanced = fresh
			? d.egressFrames > 0
			: d.egressFrames > (this.deliveredBaseline as { egressFrames: number }).egressFrames;
		this.deliveredBaseline = { epoch: d.epoch, chunksEnded: d.chunksEnded, egressFrames: d.egressFrames };
		if (egressAdvanced && !playbackAdvanced && this.st.currentTransportEpoch !== null) {
			this.dispatch({
				kind: 'modelEvent',
				at: input.at,
				transportEpoch: this.st.currentTransportEpoch,
			});
		}
		const wasNonIdle = this.st.phase !== 'idle' || this.st.origin !== null;
		if (
			playbackAdvanced &&
			this.st.clientAttached &&
			this.st.currentTransportEpoch !== null &&
			this.st.currentClientEpoch !== null
		) {
			if (wasNonIdle) {
				this.record({
					row: 'first-user-visible-response',
					phaseBefore: this.st.phase,
					attemptEpoch: this.st.attemptEpoch,
					at: input.at,
				});
			}
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
