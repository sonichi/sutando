/** TypeScript twin of src/local_api_gate.py. */

import { closeSync, fstatSync, linkSync, mkdirSync, openSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';
import type { IncomingHttpHeaders } from 'node:http';
import { dirname, join } from 'node:path';

import { resolveWorkspace, statusReadPath } from './workspace_default.js';

export const LOCAL_API_TOKEN_NAME = 'agent-api.token';

function readPublishedToken(path: string): string {
	const delay = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
	for (let attempt = 0; attempt < 8; attempt++) {
		try {
			const value = readFileSync(path, 'utf8').trim();
			if (value) return value;
		} catch {}
		if (attempt < 7) Atomics.wait(delay, 0, 0, 5);
	}
	throw new Error('local API token publication did not complete');
}

export function localApiToken(workspace = resolveWorkspace(), configured = process.env.SUTANDO_API_TOKEN ?? ''): string {
	if (configured) return configured;
	const path = statusReadPath(LOCAL_API_TOKEN_NAME, workspace);
	let emptyFileIdentity: { dev: number; ino: number } | null = null;
	try {
		const fd = openSync(path, 'r');
		try {
			const value = readFileSync(fd, 'utf8').trim();
			if (value) return value;
			const { dev, ino } = fstatSync(fd);
			emptyFileIdentity = { dev, ino };
		} finally {
			closeSync(fd);
		}
	} catch {
		// Mint a token when no readable token exists.
	}
	if (emptyFileIdentity) {
		try {
			const current = statSync(path);
			if (current.dev === emptyFileIdentity.dev && current.ino === emptyFileIdentity.ino) unlinkSync(path);
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
		}
	}

	const value = randomBytes(32).toString('base64url');
	mkdirSync(dirname(path), { recursive: true });
	const temp = join(dirname(path), `.${LOCAL_API_TOKEN_NAME}.${randomBytes(8).toString('hex')}.tmp`);
	const fd = openSync(temp, 'wx', 0o600);
	try {
		writeFileSync(fd, `${value}\n`, 'utf8');
	} finally {
		closeSync(fd);
	}
	let linkError: unknown;
	try {
		linkSync(temp, path);
	} catch (error) {
		linkError = error;
	} finally {
		try { unlinkSync(temp); } catch {}
	}
	if (linkError) {
		let collision = (linkError as NodeJS.ErrnoException).code === 'EEXIST';
		if (!collision) {
			try {
				statSync(path);
				collision = true;
			} catch {}
		}
		if (!collision) throw linkError;
	}
	return readPublishedToken(path);
}

export function bearerMatches(authorization: string | string[] | undefined, expected: string): boolean {
	const header = Array.isArray(authorization) ? authorization[0] : authorization;
	let presented = (header ?? '').trim();
	if (/^Bearer /i.test(presented)) presented = presented.slice(7).trim();
	if (!presented || !expected) return false;
	const actualBytes = Buffer.from(presented);
	const expectedBytes = Buffer.from(expected);
	return actualBytes.length === expectedBytes.length && timingSafeEqual(actualBytes, expectedBytes);
}

function normalizedHostname(hostname: string): string {
	return hostname.toLowerCase().replace(/^\[|\]$/g, '');
}

function loopbackHostname(hostname: string): boolean {
	const host = normalizedHostname(hostname);
	return host === 'localhost' || host === '127.0.0.1' || host === '::1' || host.endsWith('.localhost');
}

export function isLoopbackOrigin(origin: string | undefined): boolean {
	if (!origin) return false;
	try {
		const parsed = new URL(origin);
		return (parsed.protocol === 'http:' || parsed.protocol === 'https:')
			&& loopbackHostname(parsed.hostname);
	} catch {
		return false;
	}
}

const FORWARDED_HEADERS = [
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
] as const;

export function directLoopbackBearerRouteAllowed(
	remoteAddress: string | undefined,
	host: string | undefined,
	origin: string | undefined,
	headers: IncomingHttpHeaders,
): boolean {
	const target = host ? hostNameFromHeader(host) : null;
	if (!isLoopbackAddress(remoteAddress)) return false;
	if (!target || !loopbackHostname(target.hostname) || !origin) return false;
	try {
		const parsedOrigin = new URL(origin);
		if ((parsedOrigin.protocol !== 'http:' && parsedOrigin.protocol !== 'https:')
			|| parsedOrigin.host.toLowerCase() !== target.authority
			|| !loopbackHostname(parsedOrigin.hostname)) return false;
	} catch {
		return false;
	}
	return !FORWARDED_HEADERS.some(header => headers[header] !== undefined);
}

function hostNameFromHeader(host: string): { hostname: string; authority: string } | null {
	try {
		const parsed = new URL(`http://${host}`);
		if (!parsed.hostname || parsed.username || parsed.password || parsed.pathname !== '/') return null;
		return { hostname: normalizedHostname(parsed.hostname), authority: parsed.host.toLowerCase() };
	} catch {
		return null;
	}
}

export function browserRequestAllowed(origin: string | undefined, host: string | undefined): boolean {
	if (!origin) return true;
	if (!host) return false;
	try {
		const parsedOrigin = new URL(origin);
		const target = hostNameFromHeader(host);
		if (!target || parsedOrigin.host.toLowerCase() !== target.authority) return false;
		return loopbackHostname(target.hostname) || isIP(target.hostname) !== 0 || parsedOrigin.protocol === 'https:';
	} catch {
		return false;
	}
}

export function isLoopbackAddress(remoteAddress: string | undefined): boolean {
	const address = (remoteAddress ?? '').toLowerCase();
	return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1';
}

export type VoiceConnectionDecision =
	| { ok: true }
	| { ok: false; code: number; reason: string };

export function voiceConnectionDecision(
	req: { url?: string; headers: IncomingHttpHeaders; socket?: { remoteAddress?: string } },
	token: string,
): VoiceConnectionDecision {
	let url: URL;
	try {
		url = new URL(req.url ?? '/', 'ws://localhost');
	} catch {
		return { ok: false, code: 4401, reason: 'unauthorized' };
	}
	const originHeader = req.headers.origin;
	const origin = Array.isArray(originHeader) ? originHeader[0] : originHeader;
	if (origin && !isLoopbackOrigin(origin)) return { ok: false, code: 4403, reason: 'origin not allowed' };
	if (url.searchParams.get('probe') === '1') return { ok: true };
	const accessToken = url.searchParams.get('access_token') ?? undefined;
	if (bearerMatches(req.headers.authorization, token) || bearerMatches(accessToken, token)) {
		return { ok: true };
	}
	// A presented credential that failed never falls through to the local escape.
	if (req.headers.authorization !== undefined || accessToken !== undefined) {
		return { ok: false, code: 4401, reason: 'unauthorized' };
	}
	// Native local clients (the desktop app) send no Origin and no bearer.
	if (!origin && isLoopbackAddress(req.socket?.remoteAddress)) return { ok: true };
	return { ok: false, code: 4401, reason: 'unauthorized' };
}

export function installVoiceConnectionGate(session: unknown, tokenProvider: () => string): void {
	type Socket = {
		on(event: string, listener: () => void): unknown;
		close(code: number, reason: string): unknown;
	};
	type Request = { url?: string; headers: IncomingHttpHeaders; socket?: { remoteAddress?: string } };
	type Transport = {
		handleConnection?: (ws: Socket, req: Request) => unknown;
	};
	const transport = (session as { clientTransport?: Transport } | null)?.clientTransport;
	if (typeof transport?.handleConnection !== 'function') {
		throw new Error('voice WS gate seam is missing in bodhi-realtime-agent');
	}
	const original = transport.handleConnection;
	transport.handleConnection = function (ws: Socket, req: Request): unknown {
		let token = '';
		try {
			if (new URL(req.url ?? '/', 'ws://localhost').searchParams.get('probe') !== '1') token = tokenProvider();
		} catch {
			token = '';
		}
		const decision = voiceConnectionDecision(req, token);
		if (!decision.ok) {
			ws.on('error', () => {});
			ws.close(decision.code, decision.reason);
			console.warn(`[voice-agent] voice WS refused: ${decision.code} ${decision.reason}`);
			return;
		}
		return original.call(transport, ws, req);
	};
}
