// Host-initiated upstream recovery against bodhi >= 0.4's public recovery contract.
// voice-agent.ts owns when to dial; this module owns how, so the calls themselves are testable.

import type { RecoverUpstreamArgs, RecoverUpstreamResult } from 'bodhi-realtime-agent';

/** The part of a VoiceSession these helpers touch; older runtimes may lack the recovery API. */
export interface RecoverySurface {
	sessionManager?: { state?: unknown; transitionTo?: (state: string) => void };
	getRecoveryCapabilities?: () => { recoverUpstream?: boolean } | undefined;
	recoverUpstream?: (args: RecoverUpstreamArgs) => Pick<RecoverUpstreamResult, 'activated'>;
	handleClientConnected?: () => void;
}

export interface RecoveryLog {
	log: (msg: string) => void;
	error: (msg: string, err?: unknown) => void;
}

function canRecover(s: RecoverySurface): boolean {
	return s.getRecoveryCapabilities?.()?.recoverUpstream === true && typeof s.recoverUpstream === 'function';
}

function recover(
	s: RecoverySurface,
	reason: RecoverUpstreamArgs['reason'],
	origin: string,
	out: RecoveryLog,
	opts?: { hold?: boolean; onActivated?: () => void },
): void {
	const r = s.recoverUpstream!({ reason, skipContextInjection: false, holdSyntheticUntilFreshSpeech: opts?.hold ?? false });
	r.activated
		.then(() => opts?.onActivated?.())
		.catch((err: unknown) => out.error(`[${origin}] recoverUpstream did not activate:`, (err as Error)?.message ?? err));
}

/**
 * Redial a down upstream: UPSTREAM_LOST via recoverUpstream(), anything else via `legacy`
 * (the cast handleClientConnected() reconnect). Without `legacy`, an unparked session is left alone.
 */
export function redialUpstream(
	s: RecoverySurface | null | undefined,
	opts: {
		origin: string;
		reason: RecoverUpstreamArgs['reason'];
		legacy?: (dial: () => void) => void;
		hold?: boolean;
		onActivated?: () => void;
	} & RecoveryLog,
): 'recover' | 'legacy' | 'none' {
	if (!s) return 'none';
	if (String(s.sessionManager?.state ?? 'unknown') === 'UPSTREAM_LOST' && canRecover(s)) {
		opts.log(`[${opts.origin}] recoverUpstream(${opts.reason}) from UPSTREAM_LOST`);
		try {
			recover(s, opts.reason, opts.origin, opts, opts);
		} catch (err) {
			opts.error(`[${opts.origin}] recoverUpstream threw:`, (err as Error)?.message ?? err);
		}
		return 'recover';
	}
	if (!opts.legacy) return 'none';
	try {
		opts.legacy(() => s.handleClientConnected?.());
	} catch (err) {
		opts.error(`[${opts.origin}] reconnect trigger failed:`, (err as Error)?.message ?? err);
	}
	return 'legacy';
}

/**
 * Replace a dial hung in CONNECTING: recoverUpstream() abandons it without finalizing the session;
 * without that capability the state is forced to CLOSED. Returns whether the hang was handled.
 */
export function replaceHungDial(s: RecoverySurface, stuckForS: number, out: RecoveryLog): boolean {
	if (canRecover(s)) {
		out.error(`[Health] Stuck in CONNECTING for ${stuckForS}s — recoverUpstream() replaces the hung dial`);
		try {
			s.recoverUpstream!({ reason: 'human-retry', skipContextInjection: false, holdSyntheticUntilFreshSpeech: false })
				.activated.catch((err: unknown) => out.error('[Health] recoverUpstream did not activate:', (err as Error)?.message ?? err));
			return true;
		} catch (err) {
			out.error(`[Health] recoverUpstream threw (state=${String(s.sessionManager?.state)}):`, (err as Error)?.message ?? err);
			return false;
		}
	}
	out.error(`[Health] Stuck in CONNECTING for ${stuckForS}s — forcing CLOSED to recover`);
	try {
		s.sessionManager?.transitionTo?.('CLOSED');
		return true;
	} catch (err) {
		out.error(`[Health] Could not force CLOSED (state=${String(s.sessionManager?.state)}):`, (err as Error)?.message ?? err);
		return false;
	}
}

/**
 * The host's one dial decision, with the session lookup inside so a delegation test can drive it:
 * a mutation that stops resolving the session fails here rather than passing unseen at a call site.
 */
export function createUpstreamRedialer(
	deps: { getSession: () => RecoverySurface | null | undefined; legacy: (dial: () => void) => void } & RecoveryLog,
): (origin: string, reason?: RecoverUpstreamArgs['reason']) => 'recover' | 'legacy' | 'none' {
	return (origin, reason = 'human-retry') =>
		redialUpstream(deps.getSession(), { origin, reason, legacy: deps.legacy, log: deps.log, error: deps.error });
}

/**
 * The CONNECTING watchdog's decision. Returns whether the caller should clear its stuck-since clock;
 * owning the `forceClose` branch here is what puts it under test.
 */
export function onConnectingTick(
	args: { forceClose: boolean; session: RecoverySurface; stuckForS: number } & RecoveryLog,
): boolean {
	if (!args.forceClose) return false;
	return replaceHungDial(args.session, args.stuckForS, args);
}

/**
 * A pull-side adapter's redial after bodhi's own resumption retries are spent: recoverUpstream is the
 * only path, and `isLive` gates it so a call that hung up or went away is never redialled.
 */
export function createPostParkRedialer(
	deps: {
		getSession: () => RecoverySurface | null | undefined;
		isLive: () => boolean;
		origin: string;
		reason?: RecoverUpstreamArgs['reason'];
		hold?: boolean;
		onActivated?: () => void;
	} & RecoveryLog,
): () => 'recover' | 'none' | 'skipped' {
	return () => {
		if (!deps.isLive()) return 'skipped';
		const path = redialUpstream(deps.getSession(), {
			origin: deps.origin,
			reason: deps.reason ?? 'human-retry',
			hold: deps.hold,
			onActivated: deps.onActivated,
			log: deps.log,
			error: deps.error,
		});
		return path === 'recover' ? 'recover' : 'none';
	};
}
