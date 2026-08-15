// Shared request-body cap for the two HTTP surfaces that accept a vision frame:
// the web-client's /vision/frame proxy and the voice-agent's vision control
// server. Both buffer a binary body, so both need the same ceiling — and a cap
// that lives in one adapter is a cap the other one silently lacks.

import type { IncomingMessage } from 'node:http';

/** Largest body either vision surface will buffer.
 *
 *  A bounded frame is ~236 KB and an unbounded Retina capture ~2.5 MB, so this
 *  clears real traffic by a wide margin while keeping a single request from
 *  pinning arbitrary memory. The web-client binds 0.0.0.0 by default, which is
 *  what makes the ceiling load-bearing rather than tidy.
 */
export const FRAME_MAX_BODY_BYTES = 4 * 1024 * 1024;

/** Buffer a request body, refusing anything over `max`.
 *
 *  Resolves the body, or `null` when the cap is exceeded or the request errors.
 *  The check runs on every chunk and destroys the request as soon as the cap is
 *  passed: deciding at `end` is precisely what lets an unbounded body
 *  accumulate first.
 */
export function readBodyCapped(
	req: IncomingMessage,
	max: number = FRAME_MAX_BODY_BYTES,
): Promise<Buffer | null> {
	return new Promise((resolve) => {
		const chunks: Buffer[] = [];
		let total = 0;
		let settled = false;
		const finish = (value: Buffer | null): void => {
			if (settled) return;
			settled = true;
			resolve(value);
		};
		req.on('data', (c: Buffer) => {
			if (settled) return;
			total += c.byteLength;
			if (total > max) {
				// Refuse before this chunk is retained, then stop the producer —
				// resolving without destroying would leave it free to keep sending.
				finish(null);
				req.destroy();
				return;
			}
			chunks.push(c);
		});
		req.on('end', () => finish(Buffer.concat(chunks)));
		req.on('error', () => finish(null));
	});
}
