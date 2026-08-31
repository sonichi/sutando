/**
 * Transparent voice-connection tier resolution — picks the best reachable
 * endpoint for "call your agent" so the user never chooses a tier.
 *
 * The candidate order encodes the tier ladder, best/fastest first:
 *   local (T0) → direct via Tailscale/LAN (T1–2) → relay/Element (T3).
 *   Canonical taxonomy: docs/voice-tiers.md.
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

export type VoiceTier = 'local' | 'tailnet' | 'lan' | 'relay' | 'cloud';

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
		let ok: boolean;
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
 * is skipped (e.g. no tailnet host known → no tailnet candidate). `local` is
 * always first (best) and `cloud` last (the always-available fallback).
 *
 * Two distinct ports matter:
 *  - `localPort` (default 9900) is the voice-agent WS, bound to loopback on the
 *    core — reachable ONLY from the same machine, so it backs the `local` tier.
 *  - `proxyPort` (default 8080) is the core webUI's opt-in `/ws` proxy (guarded
 *    by SUTANDO_LAN_SHARE, sonichi/sutando#2021). Off-localhost callers can't
 *    reach loopback:9900, so tailnet/LAN candidates go through `:proxyPort/ws`.
 *
 * `tailnet` sits directly after `local`: a mesh-VPN (Tailscale) address is a
 * superset of LAN — it reaches the core on the same network AND remotely, with a
 * stable address — so it's preferred over the same-subnet-only `lan` path.
 *
 * A browser on an HTTPS page blocks insecure `ws://` to a non-localhost host
 * (mixed content), so a directly-reachable core must be reached over `wss://`.
 * The core advertises this: when it's fronted by `tailscale serve` (TLS on the
 * MagicDNS name, sonichi/sutando#2035) it publishes an `https://<host>` endpoint,
 * and `directWsUrl` turns that into `wss://<host>/ws`. A plain `http://host:port`
 * or bare host stays `ws://…/ws` (native/non-browser clients); from a hosted page
 * that probe simply fails and the resolver falls through — safe either way.
 */
export function directWsUrl(endpoint: string, proxyPort: number): string {
	// https://<host>  → wss://<host>/ws  (tailscale serve fronts :443, no port)
	if (/^https:\/\//i.test(endpoint)) {
		return `wss://${endpoint.replace(/^https:\/\//i, '').replace(/\/+$/, '')}/ws`;
	}
	// http://<host>[:port] → ws://<host>[:port]/ws  (preserve the advertised port)
	if (/^http:\/\//i.test(endpoint)) {
		return `ws://${endpoint.replace(/^http:\/\//i, '').replace(/\/+$/, '')}/ws`;
	}
	// bare host → assume the plain /ws proxy on proxyPort
	return `ws://${endpoint}:${proxyPort}/ws`;
}

export function defaultCandidates(opts: {
	localPort?: number;     // default 9900 (loopback voice-agent WS → local tier)
	proxyPort?: number;     // default 8080 (webUI /ws proxy → off-localhost tiers)
	tailnetHost?: string;   // "host.ts.net" | "https://host.ts.net" — mesh-VPN core (preferred)
	lanHost?: string;       // "192.168.1.20" | "http://192.168.1.20:8080" — same-wifi core
	relayWsUrl?: string;    // wss://relay/.../<agent> — Tier 2b bridge to the core
	cloudUrl?: string;      // the Tier-1 cloud fallback
	agentEndpoint?: string;  // target room-agent's advertised endpoint (https://<magicdns>) — identity-routed, wins over local
}): VoiceEndpoint[] {
	const port = opts.localPort ?? 9900;
	const proxyPort = opts.proxyPort ?? 8080;
	// Identity-routed: a specific room-agent advertised its endpoint. Dial THAT agent
	// and never the generic local host (which would answer as the wrong agent). Element
	// Call (relay/cloud) stays a valid fallback since it routes to the same agent.
	if (opts.agentEndpoint) {
		const ep: VoiceEndpoint[] = [
			{ tier: 'tailnet', url: directWsUrl(opts.agentEndpoint, proxyPort), label: "this room's agent, direct over Tailscale (T1–2)" },
		];
		if (opts.relayWsUrl) ep.push({ tier: 'relay', url: opts.relayWsUrl, label: 'relay to the agent (T3)' });
		if (opts.cloudUrl) ep.push({ tier: 'cloud', url: opts.cloudUrl, label: 'relay via Element (T3)' });
		return ep;
	}
	const out: VoiceEndpoint[] = [
		{ tier: 'local', url: `ws://localhost:${port}`, label: 'this machine (T0 · local)' },
	];
	if (opts.tailnetHost) out.push({ tier: 'tailnet', url: directWsUrl(opts.tailnetHost, proxyPort), label: 'your core, direct over Tailscale (T1–2)' });
	if (opts.lanHost) out.push({ tier: 'lan', url: directWsUrl(opts.lanHost, proxyPort), label: 'same network, direct (T1 · LAN)' });
	if (opts.relayWsUrl) out.push({ tier: 'relay', url: opts.relayWsUrl, label: 'relay to your core (T3)' });
	if (opts.cloudUrl) out.push({ tier: 'cloud', url: opts.cloudUrl, label: 'relay via Element (T3)' });
	return out;
}
