/**
 * Transparent voice-connection tier resolution — picks the best reachable
 * endpoint for "call your agent" so the user never chooses a tier.
 *
 * The candidate order encodes the tier ladder, best/fastest first:
 *   local core on this machine (Tier 0) → same-LAN core (Tier 0-LAN) →
 *   relay to the core (Tier 2b) → cloud daemon (Tier 1, always-available).
 * Each is PROBED in order; the first reachable wins. This is the "user never
 * picks a tier" behaviour: the app resolves the best available path invisibly
 * and only the latency/capability differs — never the agent's behaviour.
 *
 * Framework-agnostic (no DOM/framework deps): the reachability probe is INJECTED
 * (`opts.probe`), so the SAME resolver can back multiple embedding surfaces
 * (the webUI and any host-app wrapper) — one shared source, no drift. In the browser the
 * probe opens a short-lived WebSocket; opening `ws://localhost` from a public
 * HTTPS page triggers the one-time Local Network Access permission (Chrome
 * 147+), and a denied/failed probe is simply treated as "not reachable" so the
 * resolver falls through to the next tier.
 */

export type VoiceTier = 'local' | 'lan' | 'relay' | 'cloud';

export interface VoiceEndpoint {
	tier: VoiceTier;
	/** WebSocket URL to connect the bodhi voice client to. */
	url: string;
	/** Human label for status/telemetry (e.g. "this machine (Tier 0)"). */
	label: string;
}

export interface ResolveOptions {
	/** Probe an endpoint: resolve true if reachable, false/throw otherwise.
	 *  Injected so the resolver stays DOM-free — the browser passes a WS probe. */
	probe: (url: string) => Promise<boolean>;
	/** Per-probe timeout in ms (default 2500). A slow probe counts as unreachable. */
	timeoutMs?: number;
	/** Optional per-attempt callback for status UI / telemetry. */
	onAttempt?: (ep: VoiceEndpoint, ok: boolean) => void;
}

/**
 * Probe candidates in ladder order; return the first reachable, or null if none
 * are. A probe that throws or times out counts as unreachable (fall through).
 */
export async function resolveVoiceEndpoint(
	candidates: VoiceEndpoint[],
	opts: ResolveOptions,
): Promise<VoiceEndpoint | null> {
	const timeoutMs = opts.timeoutMs ?? 2500;
	for (const ep of candidates) {
		let ok = false;
		try {
			ok = await withTimeout(opts.probe(ep.url), timeoutMs);
		} catch {
			ok = false;
		}
		// onAttempt is a status-UI/telemetry hook — keep it strictly best-effort.
		// A throwing callback must NOT abort resolution, or a harmless progress
		// handler could prevent fall-through to relay/cloud.
		try {
			opts.onAttempt?.(ep, ok);
		} catch {
			/* telemetry failure is non-fatal — continue the ladder */
		}
		if (ok) return ep;
	}
	return null;
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const t = setTimeout(() => reject(new Error('probe timeout')), ms);
		p.then(
			(v) => { clearTimeout(t); resolve(v); },
			(e) => { clearTimeout(t); reject(e); },
		);
	});
}

/**
 * Build the default candidate ladder for a user's own core. Any field omitted
 * is skipped (e.g. no LAN host known → no LAN candidate). `local` is always
 * first (best) and `cloud` last (the always-available fallback).
 */
export function defaultCandidates(opts: {
	localPort?: number;     // default 9900 (the local voice-agent WS port)
	lanHost?: string;       // e.g. "192.168.1.20" — same-wifi core
	relayWsUrl?: string;    // wss://relay/.../<agent> — Tier 2b bridge to the core
	cloudUrl?: string;      // the Tier-1 cloud fallback
}): VoiceEndpoint[] {
	const port = opts.localPort ?? 9900;
	const out: VoiceEndpoint[] = [
		{ tier: 'local', url: `ws://localhost:${port}`, label: 'this machine (Tier 0)' },
	];
	if (opts.lanHost) out.push({ tier: 'lan', url: `ws://${opts.lanHost}:${port}`, label: 'same network (Tier 0-LAN)' });
	if (opts.relayWsUrl) out.push({ tier: 'relay', url: opts.relayWsUrl, label: 'relay to your core (Tier 2b)' });
	if (opts.cloudUrl) out.push({ tier: 'cloud', url: opts.cloudUrl, label: 'cloud agent (Tier 1)' });
	return out;
}
