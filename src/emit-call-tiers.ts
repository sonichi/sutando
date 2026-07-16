/**
 * Emit the core's advertisable *direct* call tiers to `state/call-tiers.json` —
 * the runtime-authored half of the availability-driven call-tier menu (Track 9).
 *
 * The desktop "Start Call" tier menu was a static "Developer — force tier"
 * prototype: it greyed Direct(Tailscale) as "n/a" even when a tailnet endpoint
 * was live, and offered Local even on a remote Pro core (owner live-test,
 * 2026-07-10). The fix is availability-driven: the CORE advertises which direct
 * endpoints it can actually offer, and the client renders the menu from that.
 *
 * This module is the core-advertise half. It composes the direct tiers from the
 * existing `reachability-endpoints.ts` detection (tailnet + LAN, both gated by
 * SUTANDO_LAN_SHARE) and writes them to a runtime-authored state file that
 * `sutando-config.sh runtime` folds into the AgentRuntime descriptor's
 * `call_tiers` field — the same runtime-authored-state pattern as `voice_ws`.
 *
 * Scope note: only the DIRECT tiers are advertised here. `local` and
 * `cloud`/`relay` are NOT — `local` is a client-relative decision (only the
 * client knows if it is co-located with this core), and cloud/relay are always
 * available and composed client-side. The core advertises only what it uniquely
 * knows: whether a directly-reachable endpoint exists right now.
 *
 * The advertisement is a HINT — the client resolver verifies reachability
 * (first-reachable-wins, per reachability-endpoints.ts), so a slightly-stale
 * entry degrades gracefully (client tries it, times out, falls back to cloud).
 * Re-emitting on a network change / core heartbeat is a documented follow-up.
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { statusPath } from './workspace_default.js';
import { directEndpoints } from './reachability-endpoints.js';

export interface CallTier {
	/** Stable machine id for the tier (client binds render logic to this). */
	tier: string;
	/** Human label for the menu row. */
	label: string;
	/** The reachable URL, or null when this tier can't be offered right now. */
	url: string | null;
	/** True iff `url` resolved — the client shows/un-greys the row only then. */
	reachable: boolean;
}

/**
 * Compose the direct call tiers from current reachability detection. Both are
 * null (reachable:false) unless SUTANDO_LAN_SHARE is opted in AND the endpoint
 * is actually detected (tailnet node online / private-LAN IPv4 present).
 */
export function composeCallTiers(): CallTier[] {
	const { tailnet, lan } = directEndpoints();
	return [
		{ tier: 'direct-tailnet', label: 'Direct (Tailscale)', url: tailnet, reachable: tailnet !== null },
		{ tier: 'direct-lan', label: 'Direct (LAN)', url: lan, reachable: lan !== null },
	];
}

export interface CallTiersFile {
	/** Unix seconds when this was written (for a freshness follow-up). */
	ts: number;
	/** Emitter pid (diagnostic; the client verifies reachability regardless). */
	pid: number;
	call_tiers: CallTier[];
}

/** Write `state/call-tiers.json` and return its path. */
export function emitCallTiers(dest: string = statusPath('call-tiers.json')): string {
	mkdirSync(dirname(dest), { recursive: true });
	const payload: CallTiersFile = {
		ts: Math.floor(Date.now() / 1000),
		pid: process.pid,
		call_tiers: composeCallTiers(),
	};
	writeFileSync(dest, JSON.stringify(payload));
	return dest;
}

/**
 * Resolve the re-emit interval (seconds) from `--interval <sec>` / `--interval=<sec>`
 * or the `SUTANDO_CALL_TIERS_INTERVAL_S` env var (arg wins). Returns null for
 * one-shot mode — the backward-compatible default when neither is set or the
 * value is not a positive integer.
 *
 * Why re-emit: reachability CHANGES after startup (tailnet node comes up, a VPN
 * connects, LAN address appears). A startup-only emit leaves the descriptor
 * advertising `reachable:false` until the next core restart, so the client's
 * Direct(Tailscale) row stays greyed even though the endpoint is now live —
 * defeating the availability-driven menu. Periodic re-emit keeps the
 * advertisement fresh; the probe is a cheap local check with its own timeout.
 */
export function parseReemitInterval(
	argv: readonly string[] = process.argv,
	env: NodeJS.ProcessEnv = process.env,
): number | null {
	let raw: string | undefined;
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === '--interval') raw = argv[i + 1];
		else if (a.startsWith('--interval=')) raw = a.slice('--interval='.length);
	}
	if (raw === undefined) raw = env.SUTANDO_CALL_TIERS_INTERVAL_S;
	if (raw === undefined) return null;
	const n = Number(raw);
	return Number.isInteger(n) && n > 0 ? n : null;
}

// Run directly (`node emit-call-tiers.js` / `tsx src/emit-call-tiers.ts`) —
// startup wires this so the descriptor has a fresh advertisement each session.
// With `--interval <sec>` (or SUTANDO_CALL_TIERS_INTERVAL_S) it stays resident
// and re-emits on that cadence so the advertisement tracks reachability changes.
if (import.meta.url === `file://${process.argv[1]}`) {
	const intervalS = parseReemitInterval();
	const dest = emitCallTiers();
	// eslint-disable-next-line no-console
	console.log(`call-tiers written: ${dest}${intervalS ? ` (re-emitting every ${intervalS}s)` : ''}`);
	if (intervalS) {
		// Ref'd interval — the timer intentionally keeps this process resident so
		// it can re-emit on cadence (startup launches it backgrounded with `&`).
		setInterval(() => {
			try {
				emitCallTiers();
			} catch (err) {
				// A transient probe failure must not kill the loop — skip this
				// tick and try again next interval (descriptor keeps last value).
				// eslint-disable-next-line no-console
				console.error(`call-tiers re-emit skipped: ${err instanceof Error ? err.message : String(err)}`);
			}
		}, intervalS * 1000);
	}
}
