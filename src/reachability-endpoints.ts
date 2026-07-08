/**
 * Direct-reachability endpoint detection (US-10, Tier 2b) — "call your agent
 * from another device and still reach YOUR core, directly, without routing
 * through the cloud."
 *
 * A remote client can't reach the core's `localhost`, so the core advertises a
 * directly-reachable URL. This module is the CORE-side detection half: compose
 * the webUI endpoint (the HTTP port, which fronts the opt-in /ws voice proxy
 * from PR #2021) on each direct path the core can offer. The advertise TRANSPORT
 * (how the endpoint reaches the remote client) is a separate piece.
 *
 * Two direct candidates, in the resolver's preferred order:
 *   1. tailnet — a mesh-VPN address (e.g. Tailscale). Superset of LAN: reaches
 *      the core from the same Wi-Fi AND from a different network entirely, with a
 *      STABLE address (doesn't change per-Wi-Fi) and NAT-traversal + encryption
 *      handled by the mesh. Preferred when present.
 *   2. lan — a same-subnet private IPv4 (RFC1918). Works only when both devices
 *      share the LAN, but needs nothing installed on the client.
 *
 * Both are gated by SUTANDO_LAN_SHARE — nothing is advertised unless the operator
 * opted into the /ws proxy that makes the core reachable off-localhost.
 */
import { networkInterfaces } from 'node:os';
import { spawnSync } from 'node:child_process';

const CLIENT_PORT = Number(process.env.CLIENT_PORT) || 8080;

/** True when sharing the core off-localhost is opted in (mirrors web-client.ts). */
export function lanShareEnabled(): boolean {
	return /^(1|true|yes|on)$/i.test(process.env.SUTANDO_LAN_SHARE || '');
}

// ---------------------------------------------------------------------------
// LAN (same-subnet private IPv4)
// ---------------------------------------------------------------------------

/** Is this an RFC1918 private-LAN IPv4 (10/8, 172.16/12, 192.168/16)? */
export function isPrivateLanIpv4(ip: string): boolean {
	const m = ip.split('.').map(Number);
	if (m.length !== 4 || m.some((n) => Number.isNaN(n) || n < 0 || n > 255)) return false;
	if (m[0] === 10) return true;
	if (m[0] === 172 && m[1] >= 16 && m[1] <= 31) return true;
	if (m[0] === 192 && m[1] === 168) return true;
	return false; // excludes loopback (127), link-local (169.254), CGNAT/tailnet (100.64/10), public
}

/**
 * The machine's primary private-LAN IPv4, or null if none. Deterministic:
 * lowest-named interface first. Tailnet (100.64/10) and link-local are excluded
 * here — the tailnet path is its own candidate below.
 */
export function detectLanIpv4(ifaces = networkInterfaces()): string | null {
	const candidates: string[] = [];
	for (const name of Object.keys(ifaces).sort()) {
		for (const addr of ifaces[name] || []) {
			if (addr.family === 'IPv4' && !addr.internal && isPrivateLanIpv4(addr.address)) {
				candidates.push(addr.address);
			}
		}
	}
	return candidates[0] ?? null;
}

/** LAN webUI endpoint (e.g. `http://192.168.1.5:8080`), or null. */
export function lanEndpointUrl(): string | null {
	if (!lanShareEnabled()) return null;
	const ip = detectLanIpv4();
	return ip ? `http://${ip}:${CLIENT_PORT}` : null;
}

// ---------------------------------------------------------------------------
// Tailnet (mesh VPN — Tailscale)
// ---------------------------------------------------------------------------

/**
 * Parse `tailscale status --json` output into the core's tailnet host, or null
 * when the node isn't up / has no tailnet address. Pure (no subprocess) so it's
 * unit-testable. Prefers the MagicDNS name (stable, human-readable, survives IP
 * churn) and falls back to the 100.x IPv4 when MagicDNS is unavailable.
 */
export function parseTailnetHost(status: unknown): string | null {
	if (!status || typeof status !== 'object') return null;
	const self = (status as Record<string, unknown>).Self;
	if (!self || typeof self !== 'object') return null;
	const s = self as Record<string, unknown>;
	// Only advertise when the node is actually online on the tailnet.
	if (s.Online === false) return null;
	const dns = typeof s.DNSName === 'string' ? s.DNSName.replace(/\.$/, '') : '';
	if (dns) return dns;
	const ips = Array.isArray(s.TailscaleIPs) ? (s.TailscaleIPs as unknown[]) : [];
	const v4 = ips.find((ip) => typeof ip === 'string' && /^100\./.test(ip));
	return typeof v4 === 'string' ? v4 : null;
}

/**
 * The core's tailnet host via the local `tailscale` CLI, or null when tailscale
 * is absent/down. Short timeout so a hung/absent binary never blocks startup.
 * Injectable runner keeps it testable.
 */
export function detectTailnetHost(
	run: () => string | null = () => {
		try {
			const r = spawnSync('tailscale', ['status', '--json'], {
				timeout: 1500,
				encoding: 'utf8',
			});
			return r.status === 0 && r.stdout ? r.stdout : null;
		} catch {
			return null;
		}
	},
): string | null {
	const out = run();
	if (!out) return null;
	try {
		return parseTailnetHost(JSON.parse(out));
	} catch {
		return null;
	}
}

/** Tailnet webUI endpoint (e.g. `http://host.tailnet.ts.net:8080`), or null. */
export function tailnetEndpointUrl(): string | null {
	if (!lanShareEnabled()) return null;
	const host = detectTailnetHost();
	return host ? `http://${host}:${CLIENT_PORT}` : null;
}

// ---------------------------------------------------------------------------
// Combined advertisement
// ---------------------------------------------------------------------------

export interface DirectEndpoints {
	/** Mesh-VPN endpoint (Tailscale). Preferred — reaches the core off-LAN too. */
	tailnet: string | null;
	/** Same-subnet LAN endpoint. Fallback when both devices share the Wi-Fi. */
	lan: string | null;
}

/**
 * All direct endpoints the core can advertise right now, in resolver-preferred
 * order (tailnet before lan). Empty object-values when LAN sharing is off. The
 * client resolver tries these ahead of relay/cloud; first reachable wins.
 */
export function directEndpoints(): DirectEndpoints {
	return { tailnet: tailnetEndpointUrl(), lan: lanEndpointUrl() };
}
