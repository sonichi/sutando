import { createHmac, timingSafeEqual } from 'node:crypto';
import type { IncomingHttpHeaders } from 'node:http';
import type { IncomingMessage } from 'node:http';

export function twilioSignature(authToken: string, url: string, params: URLSearchParams): string {
	let signed = url;
	for (const key of [...new Set(params.keys())].sort()) {
		for (const value of params.getAll(key).sort()) signed += key + value;
	}
	return createHmac('sha1', authToken).update(signed).digest('base64');
}

export function isValidTwilioSignature(
	authToken: string,
	signature: string | undefined,
	url: string,
	params: URLSearchParams,
): boolean {
	if (!authToken || !signature || !url) return false;
	const expected = Buffer.from(twilioSignature(authToken, url, params));
	const actual = Buffer.from(signature);
	return expected.length === actual.length && timingSafeEqual(expected, actual);
}

export function verifyTwilioHttpRequest(
	authToken: string,
	signature: string | undefined,
	url: string,
	body: string,
): boolean {
	return isValidTwilioSignature(authToken, signature, url, new URLSearchParams(body));
}

export type TwilioWebhookDecision =
	| { ok: true }
	| { ok: false; status: 403 | 413; error: string };

export function twilioWebhookDecision(
	authToken: string,
	signature: string | undefined,
	url: string,
	body: string,
	tooLarge = false,
): TwilioWebhookDecision {
	if (tooLarge) return { ok: false, status: 413, error: 'Twilio request body exceeds 64 KiB' };
	if (!verifyTwilioHttpRequest(authToken, signature, url, body)) {
		return { ok: false, status: 403, error: 'invalid Twilio signature' };
	}
	return { ok: true };
}

export const MAX_TWILIO_BODY_BYTES = 64 * 1024;

export async function readTwilioBody(
	req: IncomingMessage,
	maxBytes = MAX_TWILIO_BODY_BYTES,
): Promise<{ body: string; tooLarge: boolean }> {
	const contentLength = req.headers['content-length'];
	if (typeof contentLength === 'string' && /^\d+$/.test(contentLength)
			&& Number(contentLength) > maxBytes) {
		req.pause();
		return { body: '', tooLarge: true };
	}
	return new Promise((resolve, reject) => {
		const chunks: Buffer[] = [];
		let size = 0;
		const cleanup = () => {
			req.removeListener('data', onData);
			req.removeListener('end', onEnd);
			req.removeListener('error', onError);
		};
		const onData = (chunk: Buffer | string) => {
			const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
			if (size + buffer.length > maxBytes) {
				cleanup();
				req.pause();
				resolve({ body: '', tooLarge: true });
				return;
			}
			size += buffer.length;
			chunks.push(buffer);
		};
		const onEnd = () => {
			cleanup();
			resolve({ body: Buffer.concat(chunks).toString('utf-8'), tooLarge: false });
		};
		const onError = (error: Error) => {
			cleanup();
			reject(error);
		};
		req.on('data', onData);
		req.once('end', onEnd);
		req.once('error', onError);
	});
}

export function mediaStreamUrl(webhookBase: string): string {
	return `wss://${new URL(webhookBase).host}/media-stream`;
}

export function isValidMediaStreamUpgrade(
	authToken: string,
	headers: IncomingHttpHeaders,
	webhookBase: string,
): boolean {
	if (!webhookBase) return false;
	let wsUrl: string;
	try {
		wsUrl = mediaStreamUrl(webhookBase);
	} catch {
		return false;
	}
	const signature = headers['x-twilio-signature'];
	if (typeof signature !== 'string') return false;
	const httpsUrl = wsUrl.replace(/^wss:/, 'https:');
	const params = new URLSearchParams();
	return isValidTwilioSignature(authToken, signature, wsUrl, params)
		|| isValidTwilioSignature(authToken, signature, httpsUrl, params);
}

export function mediaStreamStartCallSid(start: unknown): string | null {
	// Consistency gate only — both values arrive in the same client frame, so a
	// crafted one can match itself; the real bind is the signed upgrade + one-shot claim.
	const s = start as { callSid?: unknown; customParameters?: { callSid?: unknown } } | null;
	const nativeSid = typeof s?.callSid === 'string' ? s.callSid : null;
	const paramSid = typeof s?.customParameters?.callSid === 'string' ? s.customParameters.callSid : null;
	return nativeSid !== null && nativeSid === paramSid ? nativeSid : null;
}
