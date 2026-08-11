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
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { createHash } from 'node:crypto';
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

function readCred(): StoredClaudeOAuth | null {
	for (const service of keychainServiceCandidates()) {
		const stored = readCredFromService(service);
		if (stored) return stored;
	}
	return null;
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

// Injectable seams (keychain, refresh, upstream, clock) so the proxy's failure
// semantics are testable hermetically; defaults are the production bindings.
export interface ProxyDeps {
	readCred: () => StoredClaudeOAuth | null;
	writeCred: (service: string, oauth: ClaudeOAuth) => boolean;
	refreshAccessToken: (oauth: ClaudeOAuth) => Promise<ClaudeOAuth | null>;
	request: typeof httpsRequest;
	upstreamUrl: URL;
	updateQuotaState: (headers: Record<string, string>) => void;
	now: () => number;
	idleTimeoutMs: number;
}

export function createProxyServer(overrides: Partial<ProxyDeps> = {}) {
	const deps: ProxyDeps = {
		readCred,
		writeCred,
		refreshAccessToken,
		request: httpsRequest,
		upstreamUrl: new URL(UPSTREAM),
		updateQuotaState,
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

	// Return a usable accessToken, refreshing first if the stored one is at/near
	// expiry. Fail-safe at every step: any problem → return the existing token.
	async function getFreshOAuthToken(): Promise<string | null> {
		const stored = deps.readCred();
		if (!stored) return null;
		const { service, oauth: cred } = stored;
		const needsRefresh =
			typeof cred.expiresAt === 'number' &&
			cred.expiresAt - deps.now() <= REFRESH_SKEW_MS &&
			!!cred.refreshToken;
		if (shouldAttemptRefresh(needsRefresh, deps.now(), nextRefreshAllowedAt)) {
			if (!refreshInFlight) {
				refreshInFlight = (async () => {
					const fresh = await deps.refreshAccessToken(cred);
					if (fresh && deps.writeCred(service, fresh)) {
						refreshFailCount = 0;
						nextRefreshAllowedAt = 0;
						console.log(`${ts()} [Proxy] OAuth token refreshed (new expiry ${new Date(fresh.expiresAt ?? 0).toISOString()})`);
					} else {
						refreshFailCount += 1;
						const backoff = nextRefreshBackoffMs(refreshFailCount);
						nextRefreshAllowedAt = deps.now() + backoff;
						console.error(`${ts()} [Proxy] refresh did not persist — keeping existing token (failure ${refreshFailCount}, backing off ${Math.round(backoff / 1000)}s)`);
					}
				})().finally(() => { refreshInFlight = null; });
			}
			await refreshInFlight;
			return deps.readCred()?.oauth.accessToken ?? cred.accessToken;
		}
		return cred.accessToken;
	}

	return createServer((req, res) => {
		const chunks: Buffer[] = [];
		req.on('data', (c) => chunks.push(c));
		req.on('end', async () => {
			const body = Buffer.concat(chunks);

			// Read token fresh from keychain each request, refreshing it first if it
			// is at/near expiry (self-refresh covers headless nodes).
			const oauthToken = await getFreshOAuthToken();
			if (!oauthToken) {
				res.writeHead(502);
				res.end('No OAuth token in keychain');
				return;
			}

			const headers: Record<string, string | number | string[] | undefined> = {
				...(req.headers as Record<string, string>),
				host: deps.upstreamUrl.host,
				'content-length': body.length,
			};

			// Strip hop-by-hop headers
			delete headers['connection'];
			delete headers['keep-alive'];
			delete headers['transfer-encoding'];

			// Inject OAuth token for auth requests
			if (headers['authorization']) {
				delete headers['authorization'];
				headers['authorization'] = `Bearer ${oauthToken}`;
			}

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
						deps.updateQuotaState(quotaHeaders);
					}

					res.writeHead(upRes.statusCode!, upRes.headers);
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
		});
	});
}

// Back-compat sync reader (startup probe only — does not refresh).
function getOAuthToken(): string | null {
	return readCred()?.oauth.accessToken ?? null;
}

function updateQuotaState(headers: Record<string, string>): void {
	try {
		const state: Record<string, unknown> = {
			available: true,
			last_checked: new Date().toISOString(),
			headers,
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

		mkdirSync(dirname(QUOTA_FILE), { recursive: true });
		writeFileSync(QUOTA_FILE, JSON.stringify(state, null, 2));
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
