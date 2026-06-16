/**
 * Id minting for the observability + metering spine. Zero dependencies.
 *
 * Twin of `ids.py` — both languages MUST produce the same format (prefix +
 * length + alphabet) so an id minted on either side is interchangeable.
 *
 *   - `newTraceId()` / `newUsageId()` → ULID-style: a 48-bit millisecond
 *     timestamp + 80 bits of randomness, Crockford base32, 26 chars, with a
 *     2-char type prefix (`tr_` / `ux_`). Lexicographically TIME-SORTABLE
 *     (the time component is the high-order chars), URL-safe, collision-resistant.
 *   - `newSpanId()` → `sp_` + 16 hex chars (8 random bytes). Spans need
 *     uniqueness, not time-sortability.
 *
 * Crockford base32 omits I, L, O, U to avoid visual ambiguity. The matching
 * regex is `^(tr|ux)_[0-9A-HJKMNP-TV-Z]{26}$` and `^sp_[0-9a-f]{16}$`.
 */

import { randomBytes } from 'node:crypto';

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const TIME_LEN = 10; // 10 base32 chars = 50 bits; holds a 48-bit ms timestamp
const RAND_LEN = 16; // 16 base32 chars = 80 bits of randomness

function encodeTime(ms: number, len: number): string {
	let out = '';
	let n = Math.floor(ms);
	for (let i = 0; i < len; i++) {
		out = CROCKFORD[n % 32] + out;
		n = Math.floor(n / 32);
	}
	return out;
}

function encodeRandom(len: number): string {
	const bytes = randomBytes(len);
	let out = '';
	for (let i = 0; i < len; i++) out += CROCKFORD[bytes[i] & 31];
	return out;
}

/** Bare ULID-style body (no prefix). `at` overridable for testing/determinism. */
export function ulid(at: number = Date.now()): string {
	return encodeTime(at, TIME_LEN) + encodeRandom(RAND_LEN);
}

export function newTraceId(at?: number): string {
	return 'tr_' + ulid(at);
}

export function newUsageId(at?: number): string {
	return 'ux_' + ulid(at);
}

export function newSpanId(): string {
	return 'sp_' + randomBytes(8).toString('hex');
}
