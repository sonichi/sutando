/**
 * Media-stream binding — the two gates conversation-server installs on the
 * Twilio WebSocket, kept as named units so the wiring is directly testable.
 * verifyClient = signed-upgrade gate; mediaStreamClaim = start-frame bind +
 * one-shot claim against the record /twilio/connect stored.
 */

import type { IncomingMessage, Server } from 'node:http';
import { WebSocketServer } from 'ws';
import type { CallRecord, CallRecordStore } from './call_records.js';
import { isValidMediaStreamUpgrade, mediaStreamStartCallSid } from './twilio_request_auth.js';

export function mediaStreamVerifyClient(
	authToken: string,
	webhookBase: string,
	onReject?: (remote: string) => void,
): (info: { origin: string; secure: boolean; req: IncomingMessage }) => boolean {
	return (info) => {
		// The signature covers only the bare stream URL, so a request target
		// carrying a query is unsigned data — refuse it outright.
		const valid = (info.req.url ?? '').indexOf('?') === -1
			&& isValidMediaStreamUpgrade(authToken, info.req.headers, webhookBase);
		if (!valid) onReject?.(info.req.socket.remoteAddress ?? '?');
		return valid;
	};
}

export type MediaStreamClaim =
	| { ok: true; callSid: string; record: CallRecord }
	| { ok: false; reason: 'callSid mismatch' | 'unknown call' };

export function mediaStreamClaim(records: CallRecordStore, start: unknown): MediaStreamClaim {
	const callSid = mediaStreamStartCallSid(start);
	if (callSid === null) return { ok: false, reason: 'callSid mismatch' };
	const record = records.claim(callSid);
	if (!record) return { ok: false, reason: 'unknown call' };
	return { ok: true, callSid, record };
}

export function createMediaStreamServer(
	server: Server,
	authToken: string,
	webhookBase: string,
	onReject?: (remote: string) => void,
): WebSocketServer {
	return new WebSocketServer({
		server,
		path: '/media-stream',
		verifyClient: mediaStreamVerifyClient(authToken, webhookBase, onReject),
	});
}
