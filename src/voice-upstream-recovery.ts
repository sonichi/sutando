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

function recover(s: RecoverySurface, reason: RecoverUpstreamArgs['reason'], origin: string, out: RecoveryLog): void {
	const r = s.recoverUpstream!({ reason, skipContextInjection: false, holdSyntheticUntilFreshSpeech: false });
	r.activated.catch((err: unknown) => out.error(`[${origin}] recoverUpstream did not activate:`, (err as Error)?.message ?? err));
}

/**
 * Redial a session whose upstream is down. UPSTREAM_LOST goes through recoverUpstream();
 * anything else runs `legacy`, which wraps the cast handleClientConnected() reconnect.
 */
export function redialUpstream(
	s: RecoverySurface | null | undefined,
	opts: { origin: string; reason: RecoverUpstreamArgs['reason']; legacy: (dial: () => void) => void } & RecoveryLog,
): 'recover' | 'legacy' | 'none' {
	if (!s) return 'none';
	if (String(s.sessionManager?.state ?? 'unknown') === 'UPSTREAM_LOST' && canRecover(s)) {
		opts.log(`[${opts.origin}] recoverUpstream(${opts.reason}) from UPSTREAM_LOST`);
		try {
			recover(s, opts.reason, opts.origin, opts);
		} catch (err) {
			opts.error(`[${opts.origin}] recoverUpstream threw:`, (err as Error)?.message ?? err);
		}
		return 'recover';
	}
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
