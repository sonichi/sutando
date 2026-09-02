/**
 * `agent.state` v1 protocol provider + lifecycle snapshot publisher
 * (design 1a′; impl plan WS1 Step 12, amendments R8/A9/A10/S3/Z3).
 *
 * This module owns the pure/testable pieces of the Step-12 emitter:
 *
 *  - `createAgentStateProvider()` — assembles the versioned
 *    `{type:'agent.state', v:1, …}` frame from live getters supplied by
 *    voice-agent.ts (bodhi session state, client attachment, fatal-backoff
 *    deadline, the classifier's persisted terminal classification, and the
 *    credential resolver result). voice-agent passes the provider's `build`
 *    to bodhi as the `probeState` option (when the pin supports it), sends
 *    the frame on every accepted real connection, and re-sends on every
 *    upstream transition.
 *  - `publishLifecycleSnapshot()` — the ONE writer (amendment A9) of
 *    `<workspace>/state/voice-lifecycle.json`, atomically via
 *    temp-file + `renameSync` with unique temp names. `writeVoiceState`'s
 *    plain `writeFileSync` is explicitly NOT the pattern here: WS2's control
 *    consumer reads this file cross-process and must never see a torn write.
 *  - `createIsolatedIdleRestore()` — amendment Z3's isolated idle-teardown
 *    timer: the initial idle timer in voice-agent is one-shot and rearmed
 *    only by the REAL-client disconnect wrapper, so a verifier/probe that
 *    observes (or wakes) the upstream after that timer already fired would
 *    otherwise leave the upstream connected forever. On verifier/probe close
 *    with no real client attached, this fence restores the prior idle state
 *    — and a later real connection fences (invalidates) any pending restore.
 *
 * The frame schema is WIRE CONTRACT v1 (design 1a′). Do not add/rename
 * fields without a version bump.
 */
import { mkdirSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { statusPath } from './workspace_default.js';
import type { ProtocolFailure, ProtocolFailureCategory } from './voice-error-classifier.js';
import type { ResolvedCredential } from './credential-resolver.js';
import { credentialSourceLabel as resolverSourceLabel } from './credential-resolver.js';

/** L3 upstream states (design readiness model). */
export type UpstreamState = 'live' | 'idle' | 'connecting' | 'backoff' | 'failed';

/** `agent.state` v1 frame — design 1a′, verbatim schema. */
export interface AgentStateV1 {
	type: 'agent.state';
	v: 1;
	initialized: boolean;
	upstream: UpstreamState;
	reason?: string;
	category?: ProtocolFailureCategory;
	clientAttached: boolean;
	credentialSource?: 'managed' | 'byok';
	credentialGeneration?: string;
	launchdContract?: 1;
}

/**
 * Map the resolver's source labels onto the frame's enum (amendment A10:
 * source comes from the EXISTING `voiceCredential.source`). The vocabulary
 * mapping itself ('env' means BYOK on the wire) has ONE definition — the
 * resolver's `credentialSourceLabel` (WS2 Step 3); this wrapper only turns
 * its 'none' into `undefined` because the frame OMITS the field.
 */
export function credentialSourceLabel(
	source: ResolvedCredential['source'],
): 'managed' | 'byok' | undefined {
	const label = resolverSourceLabel(source);
	return label === 'none' ? undefined : label;
}

/** Inputs the provider polls at build time — all late-bound getters. */
export interface AgentStateInputs {
	/** L2: tools loaded + VoiceSession constructed + WS server listening. */
	initialized: () => boolean;
	/** bodhi `session.sessionManager.state` (CREATED/CONNECTING/ACTIVE/…). */
	sessionState: () => string;
	/** Real clients only — probe/verifier sockets never attach. */
	clientAttached: () => boolean;
	/** voice-agent's `voiceFatalBackoffUntil` (ms epoch; 0 = none). */
	backoffUntil: () => number;
	/** Classifier-persisted last terminal classification (R8), or null. */
	lastTerminalFailure: () => ProtocolFailure | null;
	/** The resolver result the agent actually loaded its key from (A10). */
	credential: () => Pick<ResolvedCredential, 'source' | 'credentialGeneration'>;
	/** SUTANDO_VOICE_LAUNCHD_CONTRACT=1 marker present in the env (R17). */
	launchdContract: () => boolean;
	now?: () => number;
}

/**
 * Pure upstream mapping (impl plan Step 12):
 *   ACTIVE → 'live';
 *   CONNECTING / RECONNECTING / TRANSFERRING → 'connecting';
 *   terminal classification persisted → 'failed' + reason + category;
 *   otherwise (CLOSED/CREATED): client attached or fatal backoff pending →
 *   'backoff'; idle-teardown with no client → 'idle'.
 * A live ACTIVE session always wins over a stale terminal classification —
 * voice-agent clears the classification on the ACTIVE transition, but the
 * mapping is safe even if the clear races the build.
 */
export function mapUpstream(args: {
	sessionState: string;
	clientAttached: boolean;
	backoffUntil: number;
	terminal: ProtocolFailure | null;
	now: number;
}): { upstream: UpstreamState; reason?: string; category?: ProtocolFailureCategory } {
	const { sessionState, clientAttached, backoffUntil, terminal, now } = args;
	if (sessionState === 'ACTIVE') return { upstream: 'live' };
	if (terminal) {
		return { upstream: 'failed', reason: terminal.reason, category: terminal.category };
	}
	if (
		sessionState === 'CONNECTING'
		|| sessionState === 'RECONNECTING'
		|| sessionState === 'TRANSFERRING'
	) {
		return { upstream: 'connecting' };
	}
	// CLOSED / CREATED / unknown: upstream is down. With a client attached
	// (or a fatal-backoff window pending) the agent is waiting to reconnect;
	// with neither, this is the healthy idle-teardown fixed point.
	if (clientAttached || backoffUntil > now) return { upstream: 'backoff' };
	return { upstream: 'idle' };
}

export interface AgentStateProvider {
	build(): AgentStateV1;
}

export function createAgentStateProvider(inputs: AgentStateInputs): AgentStateProvider {
	const now = inputs.now ?? Date.now;
	return {
		build(): AgentStateV1 {
			const mapped = mapUpstream({
				sessionState: inputs.sessionState(),
				clientAttached: inputs.clientAttached(),
				backoffUntil: inputs.backoffUntil(),
				terminal: inputs.lastTerminalFailure(),
				now: now(),
			});
			const cred = inputs.credential();
			const source = credentialSourceLabel(cred.source);
			const frame: AgentStateV1 = {
				type: 'agent.state',
				v: 1,
				initialized: inputs.initialized(),
				upstream: mapped.upstream,
				clientAttached: inputs.clientAttached(),
			};
			if (mapped.reason !== undefined) frame.reason = mapped.reason;
			if (mapped.category !== undefined) frame.category = mapped.category;
			if (source !== undefined) frame.credentialSource = source;
			// The agent only REPORTS the Rust-minted opaque generation (R7/S3)
			// — legacy credentials without one omit the field.
			if (cred.credentialGeneration) frame.credentialGeneration = cred.credentialGeneration;
			// launchdContract:1 echoed only when the launchd contract env
			// marker is set (R17) — never synthesized.
			if (inputs.launchdContract()) frame.launchdContract = 1;
			return frame;
		},
	};
}

// ---------------------------------------------------------------------------
// Lifecycle snapshot — state/voice-lifecycle.json (amendment A9)
// ---------------------------------------------------------------------------

/** On-disk lifecycle snapshot schema (A9 + S3; P7 D7.1 adds inputHealth). */
export interface VoiceLifecycleSnapshot {
	at: number;
	clientAttached: boolean;
	initialized: boolean;
	upstream: UpstreamState;
	category?: ProtocolFailureCategory;
	credentialSource?: 'managed' | 'byok';
	credentialGeneration?: string;
	/** P7 D7.1 (additive): audio-input health verdict from the engine ledger —
	 *  P4's evidence ladder consumes `stalled` for its degraded rows. */
	inputHealth?: 'ok' | 'degraded' | 'stalled' | 'unknown';
}

export function voiceLifecyclePath(workspace: string): string {
	// Canonical runtime-state path resolution (workspace contract): the
	// caller supplies the resolved workspace; statusPath owns the state/
	// layout.
	return statusPath('voice-lifecycle.json', workspace);
}

export function voiceCapabilitiesPath(workspace: string): string {
	return statusPath('voice-agent.capabilities.json', workspace);
}

/**
 * Publishes the group-E capability marker the desktop supervisor gates probes on;
 * pid+lockId bind it to this acquisition, and lockId is required — an unbound marker has no consumer.
 */
export function publishCapabilitiesMarker(
	workspace: string,
	opts: { lockId: string; now?: () => number; onError?: (err: unknown) => void },
): void {
	const target = voiceCapabilitiesPath(workspace);
	const doc = {
		probeIsolation: true,
		at: (opts.now ?? Date.now)(),
		pid: process.pid,
		lockId: opts.lockId,
	};
	const tmp = `${target}-tmp-${process.pid}-${++_tmpCounter}`;
	try {
		mkdirSync(dirname(target), { recursive: true });
		writeFileSync(tmp, JSON.stringify(doc));
		renameSync(tmp, target);
	} catch (err) {
		opts.onError?.(err);
	}
}

// Unique temp names: pid + monotonic counter — two writers (or one writer's
// interleaved transitions) can never collide on the temp file, and rename()
// is atomic on the same filesystem.
let _tmpCounter = 0;

/**
 * Atomically publish the lifecycle snapshot derived from an `agent.state`
 * frame. The SINGLE writer of this file (A9) — voice-agent calls it on every
 * relevant transition (client attach/detach, initialized flip, upstream
 * change). Failure-silent by contract: a snapshot write must never take the
 * voice path down (callers log via `onError`).
 */
export function publishLifecycleSnapshot(
	workspace: string,
	frame: AgentStateV1,
	opts?: {
		now?: () => number;
		onError?: (err: unknown) => void;
		/** P7 D7.1: additive input-health verdict (see VoiceLifecycleSnapshot). */
		inputHealth?: 'ok' | 'degraded' | 'stalled' | 'unknown';
	},
): void {
	const target = voiceLifecyclePath(workspace);
	const snapshot: VoiceLifecycleSnapshot = {
		at: (opts?.now ?? Date.now)(),
		clientAttached: frame.clientAttached,
		initialized: frame.initialized,
		upstream: frame.upstream,
	};
	if (frame.category !== undefined) snapshot.category = frame.category;
	if (frame.credentialSource !== undefined) snapshot.credentialSource = frame.credentialSource;
	if (frame.credentialGeneration !== undefined) snapshot.credentialGeneration = frame.credentialGeneration;
	if (opts?.inputHealth !== undefined) snapshot.inputHealth = opts.inputHealth;
	const tmp = `${target}-tmp-${process.pid}-${++_tmpCounter}`;
	try {
		mkdirSync(dirname(target), { recursive: true });
		writeFileSync(tmp, JSON.stringify(snapshot));
		renameSync(tmp, target);
	} catch (err) {
		opts?.onError?.(err);
	}
}

// ---------------------------------------------------------------------------
// Isolated idle-teardown restore (amendment Z3)
// ---------------------------------------------------------------------------

export interface IsolatedIdleRestore {
	/**
	 * Arm the isolated restore timer. Called on verifier/probe-role close
	 * with no real client attached (SEAM NOTE: the pinned bodhi has no
	 * probe/verifier role yet — until the Step-11 pin lands, voice-agent
	 * arms this from its `probeState` callback, the only probe-shaped hook
	 * this repo controls; when bodhi's role close hook exists it should call
	 * `arm()` directly on verifier close). No-op while a real client is
	 * attached.
	 */
	arm(): void;
	/**
	 * Fence: a later REAL connection invalidates any pending restore — the
	 * restore must never tear down the upstream under a real client that
	 * connected after the probe closed.
	 */
	fence(): void;
	/** True while a restore is armed and not yet fired/fenced. */
	pending(): boolean;
}

export function createIsolatedIdleRestore(opts: {
	delayMs: number;
	clientAttached: () => boolean;
	/** Restore the prior idle state (close the upstream transport). */
	teardown: () => void | Promise<void>;
}): IsolatedIdleRestore {
	let epoch = 0;
	let timer: ReturnType<typeof setTimeout> | null = null;
	const clear = () => {
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
	};
	return {
		arm(): void {
			if (opts.clientAttached()) return;
			clear();
			const armedEpoch = epoch;
			timer = setTimeout(() => {
				timer = null;
				// Re-check the fence AND real attachment at fire time: a real
				// client that connected after arming owns the upstream now.
				if (armedEpoch !== epoch || opts.clientAttached()) return;
				void opts.teardown();
			}, opts.delayMs);
			// Never keep the process alive just for the restore timer.
			timer.unref?.();
		},
		fence(): void {
			epoch++;
			clear();
		},
		pending(): boolean {
			return timer !== null;
		},
	};
}
