/**
 * Shadow-mode host for the ACTIVE-silence recovery reducer — Phase 0a of
 * docs/design-voice-active-silence-recovery.md (desktop repo): derives
 * diagnostic events from the health tick, feeds the pure reducer in
 * chronological order, persists would-fire evidence, and never touches the
 * live session. `armed` degrades to shadow (no bodhi capability descriptor).
 */
import type { AudioHealthSnapshot } from './voice-audio-health.js';
import type { MatrixFacts } from './voice-health-matrix.js';
import {
	initialRecoveryState, parseActiveSilenceMode, parseActiveSilenceTicks,
	reduceRecovery, type ActiveSilenceMode, type RecoveryEvent, type RecoveryState,
} from './voice-active-silence-watchdog.js';
import { WatchdogLedger } from './voice-watchdog-ledger.js';

export const DETECTOR_VERSION = 'pr1-engine-diagnostic-3';
export const CAPABILITY_SET = Object.freeze({
	turnStart: false,
	routedToolOutcomes: 'settle-hook-only',
	timestampedSpeech: 'tick-coalesced',
	epochFencing: 'engine-diagnostic',
	// Diagnostic phase: all timestamps share the audio-health snapshot's
	// Date.now domain; the armed implementation migrates to monotonic time.
	clockDomain: 'wall-datenow-diagnostic',
});
export const POST_FIRE_RECOVERY_WINDOW_MS = 60_000;

export interface ShadowTickInput {
	at: number;
	sessionState: string;
	clientConnected: boolean;
	meetingMode: boolean;
	snapshot: AudioHealthSnapshot;
	facts: MatrixFacts;
}

export class VoiceWatchdogShadow {
	readonly mode: ActiveSilenceMode;
	private state: RecoveryState = initialRecoveryState();
	private readonly requiredTicks: number;
	private readonly ledger: WatchdogLedger;
	private readonly log: (line: string) => void;

	private transportEpoch = 0;
	private sawFirstActiveEdge = false;
	private lastSessionState: string | null = null;
	private lastClientEpochSeen: number | null = null;
	private lastClientConnected = false;
	private lastModelEventSeen: number | null = null;
	private lastEgressFramesSeen = 0;
	private lastAboveFloorSeen: number | null = null;
	private lastOnsetSeen: number | null = null;
	private lastMeeting = false;
	private lastFireAt: number | null = null; // episode FIRST fire only
	private awaitingProgress = false;
	private awaitingFunctional = false;
	/** toolCallId -> originating engine-diagnostic epoch. Non-background only.
	 *  Diagnostic boundary: a reused id across generations with an interleaved
	 *  new call cannot be disambiguated until bodhi carries call tokens. */
	private readonly pendingTools = new Map<string, number>();
	/** Hook events observed between ticks, replayed chronologically. */
	private hookQueue: RecoveryEvent[] = [];

	private readonly nowFn: () => number;

	constructor(o: {
		ledger: WatchdogLedger;
		voiceSessionId?: string;
		env?: NodeJS.ProcessEnv;
		log?: (line: string) => void;
		/** Injectable clock; must share the snapshot's time domain. */
		nowFn?: () => number;
	}) {
		this.nowFn = o.nowFn ?? Date.now;
		this.ledger = o.ledger;
		this.log = o.log ?? ((l) => console.log(l));
		const env = o.env ?? process.env;
		const warn = (m: string) => this.log(m);
		let mode = parseActiveSilenceMode(env.VOICE_ACTIVE_SILENCE_MODE, warn);
		this.requiredTicks = parseActiveSilenceTicks(env.VOICE_ACTIVE_SILENCE_TICKS, warn);
		if (mode === 'armed') {
			this.log(
				'[SilenceShadow] VOICE_ACTIVE_SILENCE_MODE=armed requires the bodhi recovery '
				+ 'capability descriptor, which this build does not have — falling back to shadow',
			);
			mode = 'shadow';
		}
		if (this.requiredTicks === 0) mode = 'off';
		this.mode = mode;
		this.ledger.mergeMeta({
			mode: this.mode,
			voiceSessionId: o.voiceSessionId ?? null,
			requiredTicks: this.requiredTicks,
		});
	}

	/** Tool lifecycle from session hooks. Background `work` never vetoes (its
	 *  results ride TaskBridge and must not mask a dead session). */
	/** Shadow mode's whole value is that it may be WRONG without cost. A throw
	 *  here reaches the crash-only uncaughtException handler and kills the agent. */
	private guard(where: string, fn: () => void): void {
		try {
			fn();
		} catch (err) {
			this.log(`[SilenceShadow] ${where} threw; observation degraded, session untouched: `
				+ `${(err as Error)?.message ?? err}`);
		}
	}

	noteToolCall(toolCallId: string, execution?: string): void {
		this.guard('noteToolCall', () => this.noteToolCallUnguarded(toolCallId, execution));
	}

	noteToolSettled(toolCallId: string): void {
		this.guard('noteToolSettled', () => this.noteToolSettledUnguarded(toolCallId));
	}

	noteMeetingMode(active: boolean): void {
		this.guard('noteMeetingMode', () => this.noteMeetingModeUnguarded(active));
	}

	observeTick(input: ShadowTickInput): void {
		this.guard('observeTick', () => this.observeTickUnguarded(input));
	}

	private noteToolCallUnguarded(toolCallId: string, execution?: string): void {
		if (this.mode === 'off' || execution === 'background') return;
		// Pre-first-edge calls adopt into the upcoming epoch (see observeTick).
		this.pendingTools.set(toolCallId, this.transportEpoch);
	}

	private noteToolSettledUnguarded(toolCallId: string): void {
		if (this.mode === 'off') return;
		const originEpoch = this.pendingTools.get(toolCallId);
		if (originEpoch === undefined) return; // background/unknown: ignore
		this.pendingTools.delete(toolCallId);
		if (originEpoch !== this.transportEpoch) return; // stale generation: no outcome
		// An ordinary settle is upstream progress (design: advances the anchor,
		// retains the latch); replayed chronologically at the next tick.
		this.hookQueue.push({ kind: 'toolOutcome', at: this.nowFn(), transportEpoch: this.transportEpoch });
	}

	/** Meeting-mode mutations from every site (1s request path, tool auto-
	 *  switch), not just the 30s tick — meeting-only speech must never latch. */
	private noteMeetingModeUnguarded(active: boolean): void {
		if (this.mode === 'off') return;
		this.hookQueue.push({ kind: 'meetingModeChanged', active, at: this.nowFn() });
	}

	flush(): Promise<void> {
		// Sync throw too: the shutdown site awaits this, so an escape there
		// would fault the exit path rather than merely losing evidence.
		try {
			return this.ledger.flush();
		} catch (err) {
			this.log(`[SilenceShadow] flush threw; evidence lost, exit unaffected: `
				+ `${(err as Error)?.message ?? err}`);
			return Promise.resolve();
		}
	}

	get snapshotState(): RecoveryState {
		return this.state;
	}

	private pendingToolCountForCurrentEpoch(): number {
		let n = 0;
		for (const epoch of this.pendingTools.values()) if (epoch === this.transportEpoch) n += 1;
		return n;
	}

	private lastDropReported = 0;

	private observeTickUnguarded(input: ShadowTickInput): void {
		if (this.mode === 'off') return;
		if (this.ledger.dropped > this.lastDropReported) {
			this.log(`[SilenceShadow] ledger dropped ${this.ledger.dropped - this.lastDropReported} row(s) under pressure`);
			this.lastDropReported = this.ledger.dropped;
		}
		const derived: RecoveryEvent[] = [];

		// Client lifecycle FIRST and out-of-band: a detected epoch boundary must
		// be applied before any event carried by the NEW snapshot (chronological
		// sort would otherwise clear fresh new-epoch speech with the detach).
		const epoch = input.snapshot.epoch ?? null;
		if (input.clientConnected) {
			if (!this.lastClientConnected || (epoch !== null && epoch !== this.lastClientEpochSeen)) {
				if (this.lastClientConnected && this.lastClientEpochSeen !== null) {
					this.feed({ kind: 'clientDetached', clientEpoch: this.lastClientEpochSeen, at: input.at }, input);
				}
				this.feed({ kind: 'clientAttached', clientEpoch: epoch ?? 0, at: input.at }, input);
				this.lastClientEpochSeen = epoch;
				// Rebase speech watermarks: this snapshot's speech belongs to the
				// NEW epoch and must re-derive after the attach.
				this.lastOnsetSeen = null;
				this.lastAboveFloorSeen = null;
			}
		} else if (this.lastClientConnected) {
			this.feed({
				kind: 'clientDetached',
				clientEpoch: this.lastClientEpochSeen ?? 0,
				at: input.at,
			}, input);
		}
		this.lastClientConnected = input.clientConnected;

		// Transport lifecycle (engine-diagnostic epochs).
		if (input.sessionState !== this.lastSessionState) {
			if (input.sessionState === 'ACTIVE') {
				const previousEpoch = this.transportEpoch;
				this.transportEpoch += 1;
				if (!this.sawFirstActiveEdge) {
					// Tools that started before our first observation belong to the
					// transport we are just now seeing: adopt, don't evict.
					this.sawFirstActiveEdge = true;
					for (const [id, ep] of [...this.pendingTools]) {
						if (ep === previousEpoch) this.pendingTools.set(id, this.transportEpoch);
					}
				} else {
					for (const [id, ep] of [...this.pendingTools]) {
						if (ep !== this.transportEpoch) this.pendingTools.delete(id);
					}
				}
				derived.push({
					kind: 'transportActive', transportEpoch: this.transportEpoch,
					attemptEpoch: null, at: input.at,
				});
			} else if (input.sessionState === 'CLOSED' && this.lastSessionState !== null) {
				derived.push({
					kind: 'closedObserved', transportEpoch: this.transportEpoch,
					attemptEpoch: null, at: input.at,
				});
			}
			this.lastSessionState = input.sessionState;
		}
		if (input.meetingMode !== this.lastMeeting) {
			derived.push({ kind: 'meetingModeChanged', active: input.meetingMode, at: input.at });
			this.lastMeeting = input.meetingMode;
		}

		// Snapshot-derived progress and speech, each with its own timestamp.
		const lm = input.snapshot.lastModelEventAt;
		if (lm !== null && lm !== this.lastModelEventSeen) {
			derived.push({ kind: 'modelEvent', at: lm, transportEpoch: this.transportEpoch });
			this.lastModelEventSeen = lm;
		}
		if (
			input.snapshot.egressFrames > this.lastEgressFramesSeen &&
			input.clientConnected &&
			input.snapshot.lastEgressAt !== null
		) {
			derived.push({
				kind: 'userVisibleResponse', at: input.snapshot.lastEgressAt,
				transportEpoch: this.transportEpoch,
				clientEpoch: epoch ?? 0,
				channel: 'audio-egress',
			});
		}
		this.lastEgressFramesSeen = input.snapshot.egressFrames;
		const onset = input.facts.speechObservedAt;
		if (onset !== null && onset !== this.lastOnsetSeen) {
			// A NEW utterance onset (value change), never a replay of the old one.
			derived.push({ kind: 'speechObserved', at: onset });
			this.lastOnsetSeen = onset;
		}
		const floor = input.snapshot.speech.lastAboveFloorAt;
		if (floor !== null && floor !== this.lastAboveFloorSeen) {
			derived.push({ kind: 'speechObserved', at: floor });
			this.lastAboveFloorSeen = floor;
		}

		// Merge hook-queue events and replay everything chronologically so a
		// response cannot be applied before the speech that preceded it.
		const batch = [...this.hookQueue, ...derived].sort((a, b) => {
			const ta = 'at' in a ? a.at : 0;
			const tb = 'at' in b ? b.at : 0;
			return ta - tb;
		});
		this.hookQueue = [];
		for (const ev of batch) this.feed(ev, input);

		// Deterministic retryDue synthesis (shadow has no timers).
		if (
			this.state.phase === 'waiting-retry' &&
			this.state.retryNotBefore !== null &&
			input.at >= this.state.retryNotBefore
		) {
			this.feed({ kind: 'retryDue', attemptEpoch: this.state.attemptEpoch, at: input.at }, input);
		}

		this.feed({
			kind: 'tick', at: input.at, state: input.sessionState,
			facts: input.facts, pendingToolCount: this.pendingToolCountForCurrentEpoch(),
			requiredTicks: this.requiredTicks,
		}, input);
	}

	private feed(ev: RecoveryEvent, input: ShadowTickInput): void {
		// Post-fire classification input: genuine upstream progress after a
		// would-fire, within the counterfactual window.
		if (
			this.lastFireAt !== null &&
			(ev.kind === 'modelEvent' || ev.kind === 'toolOutcome' || ev.kind === 'userVisibleResponse') &&
			ev.at > this.lastFireAt
		) {
			const deltaMs = ev.at - this.lastFireAt;
			if (this.awaitingProgress && ev.kind !== 'userVisibleResponse') {
				this.awaitingProgress = false;
				this.ledger.append({
					row: 'post-fire-progress', kindObserved: ev.kind, deltaMs,
					withinCounterfactualWindow: deltaMs <= POST_FIRE_RECOVERY_WINDOW_MS,
					shadowEvidence: 'first-fire',
					attemptEpoch: this.state.attemptEpoch,
					transportEpoch: this.state.currentTransportEpoch,
					clientEpoch: this.state.currentClientEpoch,
				});
			}
			if (this.awaitingFunctional && ev.kind === 'userVisibleResponse') {
				this.awaitingProgress = false;
				this.awaitingFunctional = false;
				this.ledger.append({
					row: 'post-fire-functional', deltaMs,
					withinCounterfactualWindow: deltaMs <= POST_FIRE_RECOVERY_WINDOW_MS,
					shadowEvidence: 'first-fire',
					attemptEpoch: this.state.attemptEpoch,
					transportEpoch: this.state.currentTransportEpoch,
					clientEpoch: this.state.currentClientEpoch,
				});
			}
		}
		const r = reduceRecovery(this.state, ev);
		this.state = r.state;
		if (r.effect === 'restart') {
			const first = this.state.episodeAttempts === 1;
			this.log(
				`[SilenceShadow] would-restart (${first ? 'first-fire' : 'synthetic-follow-up'}) `
				+ `attempt=${this.state.episodeAttempts} anchorAgeMs=${input.at - (this.state.silenceAnchorAt ?? input.at)}`,
			);
			this.ledger.append({
				row: 'would-restart',
				shadowEvidence: first ? 'first-fire' : 'synthetic-follow-up',
				attempt: this.state.episodeAttempts,
				attemptEpoch: this.state.attemptEpoch,
				transportEpoch: this.state.currentTransportEpoch,
				clientEpoch: this.state.currentClientEpoch,
				anchorAgeMs: input.at - (this.state.silenceAnchorAt ?? input.at),
				quiescenceMs:
					this.state.lastAboveFloorAt === null ? null : input.at - this.state.lastAboveFloorAt,
				sinceFirstSpeechMs:
					this.state.firstSpeechAt === null ? null : input.at - this.state.firstSpeechAt,
				atMs: input.at,
			});
			if (first) {
				this.lastFireAt = input.at; // synthetic follow-ups never move it
				this.awaitingProgress = true;
				this.awaitingFunctional = true;
			}
			const r2 = reduceRecovery(this.state, {
				kind: 'shadowRestarted', attemptEpoch: this.state.attemptEpoch, at: input.at,
			});
			this.state = r2.state;
		} else if (r.effect === 'notify-stalled') {
			this.log('[SilenceShadow] would-enter-terminal (telemetry only)');
			this.ledger.append({
				row: 'would-terminal',
				shadowEvidence: 'synthetic-follow-up',
				attemptEpoch: this.state.attemptEpoch,
				transportEpoch: this.state.currentTransportEpoch,
				clientEpoch: this.state.currentClientEpoch,
				episodeAttempts: this.state.episodeAttempts,
				atMs: 'at' in ev ? ev.at : null,
			});
		}
	}
}
