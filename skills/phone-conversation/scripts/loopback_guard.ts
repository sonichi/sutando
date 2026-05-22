// Loopback-only access guard for the phone-conversation HTTP server.
//
// The server binds to `0.0.0.0` to accommodate ngrok's local-tunnel
// client + the Twilio webhook path. That makes every endpoint LAN-
// reachable. /twilio/* paths validate Twilio's signature; the rest are
// internal control endpoints (originate calls, hang up, play audio,
// etc.) that should never be exposed to the LAN.
//
// `isLoopback(req)` is the one-line predicate used by the request
// handler to gate non-/twilio control endpoints. Extracted to its own
// module so we can unit-test the predicate without loading
// conversation-server.ts (which has heavy module-load side effects:
// Gemini SDK init, Twilio client, env validation that `exit(1)`s when
// credentials are missing).

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
