/**
 * Sutando credential proxy — intercepts Anthropic API calls to read rate limit headers.
 *
 * Based on nanoclaw's credential-proxy.ts approach:
 * - Runs as a local HTTP proxy between Claude Code and api.anthropic.com
 * - Injects OAuth credentials from macOS keychain
 * - Reads `anthropic-ratelimit-unified-*` headers from responses
 * - Writes quota state to <workspace>/state/quota-state.json for the dashboard
 *
 * Usage:
 *   npx tsx src/credential-proxy.ts              # start on port 7846
 *   ANTHROPIC_BASE_URL=http://localhost:7846 claude ...  # route Claude through proxy
 */

import { createServer, type RequestOptions } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { gunzipSync, inflateSync, brotliDecompressSync, zstdDecompressSync } from 'node:zlib';
import { statusPath } from '../../../src/workspace_default.js';

const PORT = 7846;
const UPSTREAM = 'https://api.anthropic.com';
// Idle (inactivity) timeout in ms for the upstream connection. The socket timer
// resets on every byte sent or received, so a healthy long stream never trips it
// (Anthropic's SSE ping cadence is ~25s); it only fires when the connection has
// genuinely gone dead — sleep/wake, wifi drop, gateway unreachable mid-flight.
// Node sets no default request timeout, so without this an in-flight request
// hangs forever and freezes the agent until a full app restart. Override via
// SUTANDO_PROXY_TIMEOUT_MS; default 120s.
const UPSTREAM_IDLE_TIMEOUT_MS = Number(process.env.SUTANDO_PROXY_TIMEOUT_MS) || 120_000;
// Quota state is per-user runtime state — canonical home is <workspace>/state/.
// Historically written into the skill dir; readers (dashboard.py, read-quota.py)
// keep the skill-dir path as a last-resort fallback for one release.
const QUOTA_FILE = statusPath('quota-state.json');
// Bounded ledger of upstream rejections (4xx/5xx other than the handled 401),
// persisted in quota-state.json so health-check can page on a transient one.
export const MAX_RECENT_REJECTIONS = 20;
const REJECTION_SNIPPET_BYTES = 2048;

const DECODERS: Record<string, (b: Buffer) => Buffer> = {
	gzip: gunzipSync, 'x-gzip': gunzipSync, deflate: inflateSync,
	br: brotliDecompressSync, zstd: zstdDecompressSync,
};

/** Decode a rejection body for reading. Upstream compresses whatever the client's
 *  accept-encoding asked for, so the raw bytes are not text; a failure must say so
 *  rather than emit mojibake that looks like a corrupt message from the API. */
export function decodeRejectionBody(buf: Buffer, encoding?: string): string {
	const enc = (encoding ?? '').trim().toLowerCase();
	if (!enc || enc === 'identity') return buf.toString('utf8');
	const decode = DECODERS[enc];
	if (!decode) return `<undecodable body: ${buf.length} bytes, content-encoding: ${enc}>`;
	try {
		return decode(buf).toString('utf8');
	} catch {
		// Truncated at the snippet cap, or simply not what the header claimed.
		return `<undecodable ${enc} body: ${buf.length} bytes>`;
	}
}

// OAuth self-refresh. A namespaced CLAUDE_CONFIG_DIR uses a namespaced keychain
// item (`Claude Code-credentials-<sha256(config-dir)[0..8]>`), while vanilla
// Claude Code uses `Claude Code-credentials`. Prefer the scoped item so an
// interactive `/login` in the Sutando core is the token the proxy injects, then
// fall back to the vanilla item for older/global installs. When the chosen token
// is at/near expiry, use its refreshToken to mint a fresh one and write it back
// to the SAME keychain item. Endpoint + client_id verified from the Claude Code
// binary (v2.1.170 strings).
const TOKEN_ENDPOINT = 'https://platform.claude.com/v1/oauth/token';
const OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
const DEFAULT_KEYCHAIN_SERVICE = 'Claude Code-credentials';
// Refresh when the token expires within this window (ms). Tokens are ~8h-lived;
// 5 min of slack avoids racing the expiry on a long-running upstream request.
const REFRESH_SKEW_MS = 5 * 60 * 1000;

// Failure backoff. Once the stored token is at/near expiry, `needsRefresh` stays
// true on EVERY subsequent request — so if the refresh itself keeps failing
// (e.g. the stored refresh token was revoked/rotated out-of-band → HTTP 400),
// the naive code re-attempts on each request and hammers the OAuth endpoint in
// a tight loop. After a failure we hold off re-attempting until a backoff window
// elapses, growing exponentially from BASE to MAX and resetting on the next
// success. Overridable via env for tests / tuning.
const REFRESH_FAIL_BACKOFF_BASE_MS =
	Number(process.env.SUTANDO_PROXY_REFRESH_BACKOFF_BASE_MS) || 30 * 1000; // 30s
const REFRESH_FAIL_BACKOFF_MAX_MS =
	Number(process.env.SUTANDO_PROXY_REFRESH_BACKOFF_MAX_MS) || 15 * 60 * 1000; // 15 min

// Pure: how long to wait before the next refresh attempt after `failCount`
// consecutive failures. 0 failures → 0 (attempt immediately). Exponential
// (BASE·2^(n-1)) capped at MAX.
export function nextRefreshBackoffMs(failCount: number): number {
	if (failCount <= 0) return 0;
	return Math.min(
		REFRESH_FAIL_BACKOFF_BASE_MS * 2 ** (failCount - 1),
		REFRESH_FAIL_BACKOFF_MAX_MS,
	);
}

// Pure: should we attempt a refresh right now? Only when the token needs it AND
// we're past any active failure-backoff window. This is the guard that turns the
// per-request retry storm into at most one attempt per backoff window.
export function shouldAttemptRefresh(
	needsRefresh: boolean,
	now: number,
	nextAllowedAt: number,
): boolean {
	return needsRefresh && now >= nextAllowedAt;
}

interface ClaudeOAuth {
	accessToken: string;
	refreshToken?: string;
	expiresAt?: number; // epoch ms
	[k: string]: unknown;
}

interface StoredClaudeOAuth {
	service: string;
	oauth: ClaudeOAuth;
}

function ts(): string { return new Date().toISOString().slice(11, 23); }

export function scopedKeychainService(configDir?: string): string | null {
	const dir = (configDir ?? '').trim();
	if (!dir) return null;
	return `${DEFAULT_KEYCHAIN_SERVICE}-${createHash('sha256').update(dir).digest('hex').slice(0, 8)}`;
}

export function keychainServiceCandidates(configDir = process.env.CLAUDE_CONFIG_DIR): string[] {
	const services = [scopedKeychainService(configDir), DEFAULT_KEYCHAIN_SERVICE].filter(Boolean) as string[];
	return [...new Set(services)];
}

// Read the full cred object from one keychain service (not just accessToken).
function readCredFromService(service: string): StoredClaudeOAuth | null {
	try {
		const raw = execFileSync('security', ['find-generic-password', '-s', service, '-w'], {
			encoding: 'utf-8',
			timeout: 5000,
		}).trim();
		const parsed = JSON.parse(raw);
		const oauth = parsed?.claudeAiOauth;
		return oauth && typeof oauth.accessToken === 'string' ? { service, oauth: oauth as ClaudeOAuth } : null;
	} catch {
		return null;
	}
}

// All keychain candidates that currently hold a credential, scoped item first.
function readCredCandidates(): StoredClaudeOAuth[] {
	return keychainServiceCandidates()
		.map(readCredFromService)
		.filter((s): s is StoredClaudeOAuth => s !== null);
}

function readCred(): StoredClaudeOAuth | null {
	return readCredCandidates()[0] ?? null;
}

// Pure: pick the credential to serve. First candidate whose token is usable
// wins (scoped-first preference preserved); if none is usable, fall back to
// the first present so degraded handling (pass-through / fail-fast) sees it.
// A dead scoped item must not eclipse a fresh /login in the default item.
export function selectCred(
	candidates: StoredClaudeOAuth[],
	now: number,
	rejectedToken: string | null,
): StoredClaudeOAuth | null {
	for (const c of candidates) {
		if (classifyCredential(c.oauth, now, rejectedToken) === 'ok') return c;
	}
	return candidates[0] ?? null;
}

// Atomically write the cred back to the keychain. Returns true ONLY after a
// read-back confirms the new accessToken landed — the rotation-lockout guard:
// if we consumed a (rotating) refresh token we MUST be sure its replacement
// persisted, else the node can never refresh again.
function keychainAccount(service: string): string | null {
	try {
		const meta = execFileSync('security', ['find-generic-password', '-s', service], {
			encoding: 'utf-8', timeout: 5000,
		});
		const m = meta.match(/"acct"<blob>="([^"]*)"/);
		return m ? m[1] : null;
	} catch {
		return null;
	}
}

function writeCred(service: string, oauth: ClaudeOAuth): boolean {
	try {
		const acct = keychainAccount(service);
		if (!acct) { console.error(`${ts()} [Proxy] keychain write: account not found`); return false; }
		const payload = JSON.stringify({ claudeAiOauth: oauth });
		// execFileSync (args array, no shell) — value passed as a single argv
		// element, so no quoting/injection surface. -U updates the item in place.
		// (Value is briefly visible in `ps` to the same user — acceptable on a
		// single-user Mac, same as the rest of the vault path.)
		execFileSync('security', [
			'add-generic-password', '-U',
			'-s', service, '-a', acct, '-w', payload,
		], { timeout: 5000 });
		const back = readCredFromService(service);
		return back?.oauth.accessToken === oauth.accessToken; // rotation-lockout read-back guard
	} catch (e) {
		console.error(`${ts()} [Proxy] keychain write FAILED:`, (e as Error).message);
		return false;
	}
}

// POST the stored refresh token to the OAuth token endpoint → fresh cred.
// Fail-safe: any error returns null and the caller keeps the existing token
// (== current behavior, no regression). Request shape is standard OAuth2
// public-client refresh (JSON body); response field names tolerated in both
// snake_case (spec) and camelCase. NOT live-validated — see PR notes.
// Pure: map an OAuth token-endpoint response into a fresh cred, or null if the
// response isn't usable. Tolerates snake_case (spec) and camelCase, keeps the
// existing refresh token when the response doesn't rotate it, and refuses any
// access token that isn't a plausible non-empty string (never write garbage).
// Exported so this — the highest-risk logic (field names + the guard) — is
// unit-tested offline: no network, no keychain, no token rotation.
// Redact + truncate an upstream response body before it goes to the log.
// The OAuth token-endpoint ERROR body (e.g. `{"error":"invalid_grant",...}`)
// is what we want to see when a refresh 400s — but never risk emitting a
// token if one ever appears: mask any long token-like run (20+ base64url/JWT
// chars, incl. dotted JWTs) and cap the length. Exported for offline testing.
export function redactForLog(bodyText: string, max: number = 300): string {
	const trimmed = (bodyText ?? '').trim();
	if (!trimmed) return '(empty body)';
	const redacted = trimmed.replace(/[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{10,}){0,2}/g, '[redacted]');
	return redacted.length > max ? redacted.slice(0, max) + '…(truncated)' : redacted;
}

export function parseRefreshResponse(
	statusCode: number,
	bodyText: string,
	oauth: ClaudeOAuth,
	now: number = Date.now(),
): ClaudeOAuth | null {
	if (statusCode >= 400) return null;
	let j: Record<string, unknown>;
	try { j = JSON.parse(bodyText); } catch { return null; }
	const access = j.access_token ?? j.accessToken;
	const refresh = (j.refresh_token ?? j.refreshToken ?? oauth.refreshToken) as string | undefined;
	const expiresIn = j.expires_in ?? j.expiresIn;
	const expiresAt = (j.expires_at ?? j.expiresAt ??
		(typeof expiresIn === 'number' ? now + expiresIn * 1000 : undefined)) as number | undefined;
	if (typeof access !== 'string' || access.length < 20) return null;
	return { ...oauth, accessToken: access, refreshToken: refresh, expiresAt };
}

function refreshAccessToken(oauth: ClaudeOAuth): Promise<ClaudeOAuth | null> {
	return new Promise((resolve) => {
		if (!oauth.refreshToken) { resolve(null); return; }
		const bodyStr = JSON.stringify({
			grant_type: 'refresh_token',
			refresh_token: oauth.refreshToken,
			client_id: OAUTH_CLIENT_ID,
		});
		const u = new URL(TOKEN_ENDPOINT);
		const reqOpts: RequestOptions = {
			hostname: u.hostname,
			port: 443,
			path: u.pathname,
			method: 'POST',
			headers: {
				'content-type': 'application/json',
				'content-length': Buffer.byteLength(bodyStr),
				accept: 'application/json',
			},
		};
		const r = httpsRequest(reqOpts, (resp) => {
			const cs: Buffer[] = [];
			resp.on('data', (c) => cs.push(c));
			resp.on('end', () => {
				const rawBody = Buffer.concat(cs).toString('utf-8');
				const fresh = parseRefreshResponse(resp.statusCode ?? 0, rawBody, oauth);
				// Instrument the ACTUAL upstream failure. Previously this masked
				// every failure as "HTTP N or bad/empty response", so a recurring
				// refresh 400 (the fb556dd6 crash-loop root cause) was undiagnosable
				// — "bad/empty response" hid the real token-endpoint error. Log the
				// redacted+truncated body so the actual `error`/`error_description`
				// is visible in credential-proxy.log.
				if (!fresh) console.error(`${ts()} [Proxy] refresh unusable (HTTP ${resp.statusCode}): ${redactForLog(rawBody)}`);
				resolve(fresh);
			});
		});
		r.on('error', (e) => { console.error(`${ts()} [Proxy] refresh request error:`, e.message); resolve(null); });
		r.setTimeout(10000, () => { r.destroy(); resolve(null); });
		r.write(bodyStr);
		r.end();
	});
}

// Pure: is this credential safe to inject? 'expired' = hard expiry (the 5-min
// REFRESH_SKEW only triggers refresh attempts — a not-yet-expired token still
// works upstream). 'rejected' = upstream already 401'd this exact token, so it
// is known-dead regardless of its expiry metadata.
export function classifyCredential(
	oauth: ClaudeOAuth,
	now: number,
	rejectedToken: string | null,
): 'ok' | 'expired' | 'rejected' {
	if (rejectedToken !== null && oauth.accessToken === rejectedToken) return 'rejected';
	if (typeof oauth.expiresAt === 'number' && oauth.expiresAt <= now) return 'expired';
	return 'ok';
}

// Distinct fail-fast body: name the proxy and the remedy so a 401 from HERE is
// never mistaken for an upstream auth failure.
export function authUnavailableBody(verdict: 'expired' | 'rejected'): string {
	return JSON.stringify({
		type: 'error',
		error: {
			type: 'authentication_error',
			message:
				`sutando credential-proxy: stored OAuth token is ${verdict} and self-refresh is unavailable ` +
				'(OAuth endpoint failing; backing off). Run /login in Claude Code — the proxy re-reads the ' +
				'keychain on every request and will pick the new token up immediately.',
		},
	});
}

// Injectable seams (keychain, refresh, upstream, clock) so the proxy's failure
// semantics are testable hermetically; defaults are the production bindings.
export interface RejectionRecord {
	ts: string;
	status: number;
	path: string;
	snippet: string;
	// The proxy serves every seat on a host, so a rejection is attributable only
	// by what the request carried: the model, the CLI's user-agent, the peer port.
	model?: string;
	user_agent?: string;
	peer_port?: number;
	// What the body arrived compressed as, so an undecodable snippet says why.
	content_encoding?: string;
}

/** The `model` field of a JSON request body, or "" when absent or unparsable. */
export function requestModel(body: Buffer): string {
	try {
		const m = JSON.parse(body.toString('utf8'))?.model;
		return typeof m === 'string' ? m : '';
	} catch {
		return '';
	}
}

function isRejectionRecord(x: unknown): x is RejectionRecord {
	return !!x && typeof x === 'object'
		&& typeof (x as RejectionRecord).ts === 'string'
		&& typeof (x as RejectionRecord).status === 'number';
}

/** Append one rejection to a (possibly foreign/corrupt) prior ledger, keeping the newest `max`. */
export interface LastRequest { model: string; at: string }

/** The model that last consumed quota through this proxy, carried across
 *  requests that name none (a non-messages call must not blank the tile). */
export function withLastRequest(prev: unknown, model: string, at: string): LastRequest | undefined {
	if (model) return { model, at };
	const old = (prev as { last_request?: unknown } | null)?.last_request;
	if (old && typeof old === 'object' && typeof (old as LastRequest).model === 'string' && (old as LastRequest).model
		&& typeof (old as LastRequest).at === 'string') return old as LastRequest;
	return undefined;
}

export function appendRejection(prev: unknown, rej: RejectionRecord, max: number = MAX_RECENT_REJECTIONS): RejectionRecord[] {
	const list = Array.isArray(prev) ? prev.filter(isRejectionRecord) : [];
	list.push(rej);
	return list.slice(-max);
}

export interface ProxyDeps {
	readCredCandidates: () => StoredClaudeOAuth[];
	writeCred: (service: string, oauth: ClaudeOAuth) => boolean;
	refreshAccessToken: (oauth: ClaudeOAuth) => Promise<ClaudeOAuth | null>;
	request: typeof httpsRequest;
	upstreamUrl: URL;
	updateQuotaState: (headers: Record<string, string>, model?: string) => void;
	recordRejection: (rej: RejectionRecord) => void;
	now: () => number;
	idleTimeoutMs: number;
}

export function createProxyServer(overrides: Partial<ProxyDeps> = {}) {
	const deps: ProxyDeps = {
		readCredCandidates,
		writeCred,
		refreshAccessToken,
		request: httpsRequest,
		upstreamUrl: new URL(UPSTREAM),
		updateQuotaState,
		recordRejection,
		now: Date.now,
		idleTimeoutMs: UPSTREAM_IDLE_TIMEOUT_MS,
		...overrides,
	};
	const upstreamPort = deps.upstreamUrl.port ? Number(deps.upstreamUrl.port) : 443;

	// Single-flight guard: at most one refresh in progress, so concurrent requests
	// never race to consume/rotate the refresh token twice.
	let refreshInFlight: Promise<void> | null = null;

	// Failure-backoff state (see nextRefreshBackoffMs / shouldAttemptRefresh).
	// While now < nextRefreshAllowedAt, skip the refresh instead of hammering.
	let refreshFailCount = 0;
	let nextRefreshAllowedAt = 0;

	// Last token an upstream 401 proved dead — never injected again until a
	// refresh or /login replaces it (cleared on refresh success).
	let rejectedToken: string | null = null;

	function runSingleFlightRefresh(service: string, cred: ClaudeOAuth): Promise<void> {
		if (!refreshInFlight) {
			refreshInFlight = (async () => {
				const fresh = await deps.refreshAccessToken(cred);
				if (fresh && deps.writeCred(service, fresh)) {
					refreshFailCount = 0;
					nextRefreshAllowedAt = 0;
					rejectedToken = null;
					console.log(`${ts()} [Proxy] OAuth token refreshed (new expiry ${new Date(fresh.expiresAt ?? 0).toISOString()})`);
				} else {
					refreshFailCount += 1;
					const backoff = nextRefreshBackoffMs(refreshFailCount);
					nextRefreshAllowedAt = deps.now() + backoff;
					console.error(`${ts()} [Proxy] refresh failed (failure ${refreshFailCount}, next attempt allowed in ${Math.round(backoff / 1000)}s)`);
				}
			})().finally(() => { refreshInFlight = null; });
		}
		return refreshInFlight;
	}

	// Re-read the keychain (every call — no cache, so /login lands within one
	// request) and refresh first when at/near expiry or upstream-rejected.
	// selectCred scans ALL candidate items: a fresh /login in the default item
	// must be found even while a dead scoped item exists.
	async function resolveCredential(): Promise<StoredClaudeOAuth | null> {
		const stored = selectCred(deps.readCredCandidates(), deps.now(), rejectedToken);
		if (!stored) return null;
		const { service, oauth: cred } = stored;
		const nearExpiry =
			typeof cred.expiresAt === 'number' &&
			cred.expiresAt - deps.now() <= REFRESH_SKEW_MS;
		const needsRefresh =
			(nearExpiry || classifyCredential(cred, deps.now(), rejectedToken) !== 'ok') &&
			!!cred.refreshToken;
		if (shouldAttemptRefresh(needsRefresh, deps.now(), nextRefreshAllowedAt)) {
			if (nearExpiry) console.log(`${ts()} [Proxy] stored token at/near expiry — attempting refresh`);
			await runSingleFlightRefresh(service, cred);
			return selectCred(deps.readCredCandidates(), deps.now(), rejectedToken) ?? stored;
		}
		return stored;
	}

	// Owner's 401 state machine: reload (keychain re-read) → refresh (if the
	// backoff window allows) → rebuild → retry once. Null = give up loud.
	async function recoverAfter401(failedToken: string): Promise<string | null> {
		rejectedToken = failedToken;
		const stored = selectCred(deps.readCredCandidates(), deps.now(), rejectedToken);
		console.log(`${ts()} [Proxy] keychain re-read after 401: ${stored ? 'credential present' : 'no credential'}`);
		if (!stored) return null;
		if (
			stored.oauth.accessToken !== failedToken &&
			classifyCredential(stored.oauth, deps.now(), rejectedToken) === 'ok'
		) {
			return stored.oauth.accessToken; // a fresh /login already landed
		}
		if (stored.oauth.refreshToken && shouldAttemptRefresh(true, deps.now(), nextRefreshAllowedAt)) {
			await runSingleFlightRefresh(stored.service, stored.oauth);
			const after = selectCred(deps.readCredCandidates(), deps.now(), rejectedToken);
			if (
				after &&
				after.oauth.accessToken !== failedToken &&
				classifyCredential(after.oauth, deps.now(), rejectedToken) === 'ok'
			) {
				return after.oauth.accessToken;
			}
		}
		return null;
	}

	return createServer((req, res) => {
		const chunks: Buffer[] = [];
		req.on('data', (c) => chunks.push(c));
		req.on('end', async () => {
			const body = Buffer.concat(chunks);

			const headers: Record<string, string | number | string[] | undefined> = {
				...(req.headers as Record<string, string>),
				host: deps.upstreamUrl.host,
				'content-length': body.length,
			};

			// Strip hop-by-hop headers
			delete headers['connection'];
			delete headers['keep-alive'];
			delete headers['transfer-encoding'];

			const hasClientAuth = !!headers['authorization'] || !!headers['x-api-key'];

			// Read token fresh from keychain each request, refreshing it first if it
			// is at/near expiry (self-refresh covers headless nodes).
			const stored = await resolveCredential();
			// The token this proxy injected, if any — non-null means WE authored the
			// request's auth and own the 401-recovery duty for it.
			let injectedToken: string | null = null;
			if (!stored) {
				if (!hasClientAuth) {
					res.writeHead(502);
					res.end('No OAuth token in keychain');
					return;
				}
				console.log(`${ts()} [Proxy] no keychain credential — passing client credential through`);
			} else {
				const verdict = classifyCredential(stored.oauth, deps.now(), rejectedToken);
				if (verdict === 'ok') {
					// Inject OAuth token for auth requests
					if (headers['authorization']) {
						headers['authorization'] = `Bearer ${stored.oauth.accessToken}`;
						injectedToken = stored.oauth.accessToken;
					}
				} else if (hasClientAuth) {
					// Never inject a known-dead token over a client credential that may
					// be fresher (the /login case) — pass it through untouched.
					console.log(`${ts()} [Proxy] stored token ${verdict}, refresh unavailable — pass-through engaged (client credential forwarded untouched)`);
				} else {
					console.error(`${ts()} [Proxy] stored token ${verdict}, refresh unavailable, no client credential — failing fast (401)`);
					res.writeHead(401, { 'content-type': 'application/json' });
					res.end(authUnavailableBody(verdict));
					return;
				}
			}

			const forward = (attempt: number): void => {
				let timedOut = false;

				const upstream = deps.request(
					{
						hostname: deps.upstreamUrl.hostname,
						port: upstreamPort,
						path: req.url,
						method: req.method,
						headers,
						timeout: deps.idleTimeoutMs,
					} as RequestOptions,
					(upRes) => {
						// Extract rate limit headers
						const quotaHeaders: Record<string, string> = {};
						for (const [key, val] of Object.entries(upRes.headers)) {
							if (key.startsWith('anthropic-ratelimit')) {
								quotaHeaders[key] = String(val);
							}
						}
						if (Object.keys(quotaHeaders).length > 0) {
							console.log(`${ts()} [Quota]`, quotaHeaders);
							deps.updateQuotaState(quotaHeaders, requestModel(body));
						}

						if (upRes.statusCode === 401 && injectedToken && attempt === 0) {
							// 401 on proxy-authored auth is an auth-state transition, not a
							// generic failure: reload → refresh → retry once → give up loud.
							console.error(`${ts()} [Proxy] upstream 401 on injected token — invalidating it and re-reading keychain`);
							const buf: Buffer[] = [];
							upRes.on('data', (c) => buf.push(c));
							upRes.on('end', async () => {
								const recovered = await recoverAfter401(injectedToken as string);
								if (recovered) {
									console.log(`${ts()} [Proxy] retrying once with reloaded credential`);
									headers['authorization'] = `Bearer ${recovered}`;
									injectedToken = recovered;
									forward(1);
									return;
								}
								console.error(`${ts()} [Proxy] credential recovery failed — forwarding the upstream 401 (re-auth with /login)`);
								const h = { ...upRes.headers };
								delete h['content-length'];
								delete h['transfer-encoding'];
								res.writeHead(401, h);
								res.end(Buffer.concat(buf));
							});
							return;
						}
						if (upRes.statusCode === 401 && injectedToken && attempt > 0) {
							console.error(`${ts()} [Proxy] reloaded credential also rejected (401) — giving up`);
							rejectedToken = injectedToken;
						}

						const code = upRes.statusCode ?? 0;
						if (code >= 400 && code !== 401) {
							// The only component that sees a credits/overage rejection is this
							// proxy; without a record the dropped request is invisible upstream.
							const rejChunks: Buffer[] = [];
							let seen = 0;
							upRes.on('data', (c: Buffer) => {
								if (seen < REJECTION_SNIPPET_BYTES) { rejChunks.push(c); seen += c.length; }
							});
							upRes.on('end', () => {
								const contentEncoding = String(upRes.headers['content-encoding'] ?? '');
								// Redaction runs on the DECODED text: it cannot scrub a secret it cannot read.
								const snippet = redactForLog(decodeRejectionBody(Buffer.concat(rejChunks), contentEncoding)).slice(0, REJECTION_SNIPPET_BYTES);
								const model = requestModel(body);
								console.error(`${ts()} [Proxy] rejected HTTP ${code} on ${req.url} model=${model || '?'} peer=${req.socket.remotePort ?? '?'}: ${snippet}`);
								try {
									deps.recordRejection({
										ts: new Date(deps.now()).toISOString(), status: code, path: req.url ?? '', snippet,
						content_encoding: contentEncoding || undefined,
										model, user_agent: String(req.headers['user-agent'] ?? ''), peer_port: req.socket.remotePort,
									});
								} catch { /* best effort */ }
							});
						}
						res.writeHead(code, upRes.headers);
						upRes.pipe(res);
					},
				);

				// 'timeout' fires on socket inactivity but does NOT auto-abort — destroy the
				// request so it surfaces through the 'error' handler below as a clean failure
				// (and Claude Code's own retry kicks in) instead of hanging indefinitely.
				upstream.on('timeout', () => {
					timedOut = true;
					console.error(`${ts()} [Proxy] Upstream idle >${deps.idleTimeoutMs}ms — aborting`);
					upstream.destroy(new Error('upstream idle timeout'));
				});

				upstream.on('error', (err) => {
					console.error(`${ts()} [Proxy] Upstream error:`, err.message);
					if (!res.headersSent) {
						res.writeHead(timedOut ? 504 : 502);
						res.end(timedOut ? 'Gateway Timeout' : 'Bad Gateway');
					} else {
						// Headers already streamed — can't change status. Tear down the client
						// connection so the agent sees a broken stream and retries rather than
						// waiting forever on a dead upstream.
						res.destroy(err);
					}
				});

				// If the agent hangs up first, don't leak the in-flight upstream request.
				res.on('close', () => {
					if (!res.writableEnded) upstream.destroy();
				});

				upstream.write(body);
				upstream.end();
			};

			forward(0);
		});
	});
}

// Back-compat sync reader (startup probe only — does not refresh).
function getOAuthToken(): string | null {
	return readCred()?.oauth.accessToken ?? null;
}

function readQuotaFile(): Record<string, unknown> {
	try {
		const parsed = JSON.parse(readFileSync(QUOTA_FILE, 'utf8'));
		return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
	} catch {
		return {};
	}
}

function writeQuotaFile(state: Record<string, unknown>): void {
	mkdirSync(dirname(QUOTA_FILE), { recursive: true });
	writeFileSync(QUOTA_FILE, JSON.stringify(state, null, 2));
}

function recordRejection(rej: RejectionRecord): void {
	try {
		const prev = readQuotaFile();
		writeQuotaFile({ ...prev, recent_rejections: appendRejection(prev.recent_rejections, rej) });
	} catch { /* best effort */ }
}

function updateQuotaState(headers: Record<string, string>, model = ''): void {
	try {
		// The header write replaces the file, so carry the rejection ledger across it.
		const prev = readQuotaFile();
		const prevLedger = prev.recent_rejections;
		const lastRequest = withLastRequest(prev, model, new Date().toISOString());
		const state: Record<string, unknown> = {
			available: true,
			last_checked: new Date().toISOString(),
			headers,
			recent_rejections: Array.isArray(prevLedger) ? prevLedger.filter(isRejectionRecord) : [],
			...(lastRequest ? { last_request: lastRequest } : {}),
		};

		// Parse specific headers
		const status5h = headers['anthropic-ratelimit-unified-5h-status'];
		const util5h = headers['anthropic-ratelimit-unified-5h-utilization'];
		const reset5h = headers['anthropic-ratelimit-unified-5h-reset'];
		const util7d = headers['anthropic-ratelimit-unified-7d-utilization'];
		const reset7d = headers['anthropic-ratelimit-unified-7d-reset'];
		const overallStatus = headers['anthropic-ratelimit-unified-status'];

		if (util5h) state.utilization_5h = parseFloat(util5h);
		if (util7d) state.utilization_7d = parseFloat(util7d);
		if (reset5h) state.resets_at_5h = new Date(parseInt(reset5h) * 1000).toISOString();
		if (reset7d) state.resets_at_7d = new Date(parseInt(reset7d) * 1000).toISOString();

		if (overallStatus === 'rejected' || status5h === 'rejected') {
			state.available = false;
			state.exhausted_since = new Date().toISOString();
		}

		writeQuotaFile(state);
	} catch { /* best effort */ }
}

// Only start the server when run directly. Importing this module (e.g. from the
// offline tests) must NOT bind the port, touch the keychain, or exit.
// Match the exact basename — works for BOTH the dev entry (`credential-proxy.ts`
// run via tsx) AND the bundled artifact (`dist/credential-proxy.js`), while
// excluding the `credential-proxy-*.test.{ts,js}` files (different basenames).
const _entryName = (process.argv[1] ?? '').split(/[\\/]/).pop() ?? '';
const isMain = _entryName === 'credential-proxy.ts' || _entryName === 'credential-proxy.js';

if (isMain) {
	// Verify token exists at startup
	const initToken = getOAuthToken();
	if (!initToken) {
		console.error('No OAuth token found in macOS keychain. Is Claude Code logged in?');
		process.exit(1);
	}
	console.log(`${ts()} [Proxy] OAuth token loaded from keychain (will re-read on each request)`);

	createProxyServer().listen(PORT, '127.0.0.1', () => {
		console.log(`${ts()} [Proxy] Credential proxy → http://localhost:${PORT}`);
		console.log(`${ts()} [Proxy] Upstream: ${UPSTREAM}`);
		console.log(`${ts()} [Proxy] Set ANTHROPIC_BASE_URL=http://localhost:${PORT} to route through proxy`);
	});
}
