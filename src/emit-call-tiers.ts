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

// Run directly (`node emit-call-tiers.js` / `tsx src/emit-call-tiers.ts`) —
// startup wires this so the descriptor has a fresh advertisement each session.
if (import.meta.url === `file://${process.argv[1]}`) {
	const dest = emitCallTiers();
	// eslint-disable-next-line no-console
	console.log(`call-tiers written: ${dest}`);
}
