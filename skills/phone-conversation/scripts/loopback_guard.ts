// Loopback-only access guard for the phone-conversation HTTP server.
//
// Loopback control requests must not arrive through a forwarding tunnel.
// Kept separate from the phone server to avoid its module-load side effects.

import type { IncomingMessage } from 'node:http';

/**
 * True iff the request originated from a loopback interface — IPv4
 * `127.0.0.1`, IPv6 `::1`, or the IPv4-mapped-IPv6 form
 * `::ffff:127.0.0.1` that some Node builds report when listening on
 * IPv4 via a dual-stack socket.
 *
 * Defensive against the same bypass class as `_is_safe_callback_url`
 * in `src/agent-api.py`: a hostname/address that *resolves* to
 * loopback via IPv4-mapping must be treated as loopback.
 */
export function isLoopback(req: IncomingMessage): boolean {
	const addr = req.socket.remoteAddress ?? '';
	return addr === '127.0.0.1' || addr === '::1' || addr === '::ffff:127.0.0.1';
}

export function isForwarded(req: IncomingMessage): boolean {
	return [
		'x-forwarded-for',
		'x-forwarded-host',
		'x-forwarded-proto',
		'forwarded',
		'x-real-ip',
		'tailscale-funnel-request',
		'cf-connecting-ip',
		'true-client-ip',
		'x-forwarded-server',
		'x-original-forwarded-for',
		'via',
	].some(header => req.headers[header] !== undefined);
}

export function isDirectLoopback(req: IncomingMessage): boolean {
	return isLoopback(req) && !isForwarded(req);
}
