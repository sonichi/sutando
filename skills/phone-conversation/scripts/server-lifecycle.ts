// Lifecycle rules of the phone conversation server, kept pure so they can be
// tested without booting it (user feedback P1-19: after an engine update the
// old bundle kept running with /health saying ok, and ngrok had no supervisor).
import { statSync } from 'node:fs';

export type HealthInput = { activeCalls: number; webhookUrl: string; startedAt: number; bundlePath: string };

/** What /health reports: liveness plus what a supervisor needs to judge staleness
 *  without `ps`: when this process started and which bundle it runs. */
export function healthPayload(h: HealthInput): Record<string, unknown> {
	let bundle: { path: string; mtimeMs: number | null } = { path: h.bundlePath, mtimeMs: null };
	try {
		bundle = { path: h.bundlePath, mtimeMs: statSync(h.bundlePath).mtimeMs };
	} catch { /* a bundle that cannot be stat'ed is reported with mtime null */ }
	return { status: 'ok', activeCalls: h.activeCalls, webhookUrl: h.webhookUrl, startedAt: h.startedAt, bundle };
}

/** Endpoints that would start new call work; refused with 503 while draining.
 *  Twilio's own webhooks and hangup stay open so live calls finish. */
const DRAIN_BLOCKED = new Set(['/call', '/concurrent-call', '/meeting']);
export function isDrainBlocked(path: string, method: string | undefined, draining: boolean): boolean {
	return draining && method === 'POST' && DRAIN_BLOCKED.has(path);
}

/** How long a drain may hold a shutdown before the server exits regardless. */
export const DRAIN_CAP_MS = 10 * 60 * 1000;

/** Whether a draining server may exit now: no live calls, or the cap elapsed. */
export function drainMayExit(activeCalls: number, drainStartedAt: number, now: number, capMs = DRAIN_CAP_MS): boolean {
	return activeCalls === 0 || now - drainStartedAt >= capMs;
}

/** Delay before the Nth ngrok respawn (1-based): 2 s doubling to a 60 s ceiling. */
export function ngrokRespawnDelayMs(attempt: number): number {
	return Math.min(60_000, 2_000 * 2 ** Math.max(0, attempt - 1));
}
