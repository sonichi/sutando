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

import { createServer, request as httpRequest, type RequestOptions } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { execSync, execFileSync } from 'node:child_process';
import { writeFileSync, readFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { statusPath } from '../../../src/workspace_default.js';

const PORT = 7846;
const UPSTREAM = 'https://api.anthropic.com';
// Quota state is per-user runtime state — canonical home is <workspace>/state/.
// Historically written into the skill dir; readers (dashboard.py, read-quota.py)
// keep the skill-dir path as a last-resort fallback for one release.
const QUOTA_FILE = statusPath('quota-state.json');

// OAuth self-refresh. The proxy reads the DEFAULT `Claude Code-credentials`
// keychain item, but nothing refreshes that item's accessToken on a headless
// node (interactive `/login` refreshes it; a namespaced-CLAUDE_CONFIG_DIR core
// refreshes its OWN `Claude Code-credentials-<hash>` item). So once `expiresAt`
// passes the proxy injects an EXPIRED token → upstream 401 ("401 after a while").
// Fix: when the stored token is at/near expiry, use the stored refreshToken to
// mint a fresh one and write it back — making every proxy-routed node self-heal.
// Endpoint + client_id verified from the Claude Code binary (v2.1.170 strings).
const TOKEN_ENDPOINT = 'https://platform.claude.com/v1/oauth/token';
const OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
const KEYCHAIN_SERVICE = 'Claude Code-credentials';
// Refresh when the token expires within this window (ms). Tokens are ~8h-lived;
// 5 min of slack avoids racing the expiry on a long-running upstream request.
const REFRESH_SKEW_MS = 5 * 60 * 1000;

interface ClaudeOAuth {
	accessToken: string;
	refreshToken?: string;
	expiresAt?: number; // epoch ms
	[k: string]: unknown;
}

function ts(): string { return new Date().toISOString().slice(11, 23); }

// Read the full cred object from the keychain (not just the accessToken).
function readCred(): ClaudeOAuth | null {
	try {
		const raw = execSync(`security find-generic-password -s "${KEYCHAIN_SERVICE}" -w`, {
			encoding: 'utf-8',
			timeout: 5000,
		}).trim();
		const parsed = JSON.parse(raw);
		const oauth = parsed?.claudeAiOauth;
		return oauth && typeof oauth.accessToken === 'string' ? (oauth as ClaudeOAuth) : null;
	} catch {
		return null;
	}
}

// Atomically write the cred back to the keychain. Returns true ONLY after a
// read-back confirms the new accessToken landed — the rotation-lockout guard:
// if we consumed a (rotating) refresh token we MUST be sure its replacement
// persisted, else the node can never refresh again.
function keychainAccount(): string | null {
	try {
		const meta = execFileSync('security', ['find-generic-password', '-s', KEYCHAIN_SERVICE], {
			encoding: 'utf-8', timeout: 5000,
		});
		const m = meta.match(/"acct"<blob>="([^"]*)"/);
		return m ? m[1] : null;
	} catch {
		return null;
	}
}

function writeCred(oauth: ClaudeOAuth): boolean {
	try {
		const acct = keychainAccount();
		if (!acct) { console.error(`${ts()} [Proxy] keychain write: account not found`); return false; }
		const payload = JSON.stringify({ claudeAiOauth: oauth });
		// execFileSync (args array, no shell) — value passed as a single argv
		// element, so no quoting/injection surface. -U updates the item in place.
		// (Value is briefly visible in `ps` to the same user — acceptable on a
		// single-user Mac, same as the rest of the vault path.)
		execFileSync('security', [
			'add-generic-password', '-U',
			'-s', KEYCHAIN_SERVICE, '-a', acct, '-w', payload,
		], { timeout: 5000 });
		const back = readCred();
		return back?.accessToken === oauth.accessToken; // rotation-lockout read-back guard
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
				const fresh = parseRefreshResponse(resp.statusCode ?? 0, Buffer.concat(cs).toString('utf-8'), oauth);
				if (!fresh) console.error(`${ts()} [Proxy] refresh unusable (HTTP ${resp.statusCode} or bad/empty response)`);
				resolve(fresh);
			});
		});
		r.on('error', (e) => { console.error(`${ts()} [Proxy] refresh request error:`, e.message); resolve(null); });
		r.setTimeout(10000, () => { r.destroy(); resolve(null); });
		r.write(bodyStr);
		r.end();
	});
}

// Single-flight guard: at most one refresh in progress, so concurrent requests
// never race to consume/rotate the refresh token twice.
let refreshInFlight: Promise<void> | null = null;

// Return a usable accessToken, refreshing first if the stored one is at/near
// expiry. Fail-safe at every step: any problem → return the existing token.
async function getFreshOAuthToken(): Promise<string | null> {
	const cred = readCred();
	if (!cred) return null;
	const needsRefresh =
		typeof cred.expiresAt === 'number' &&
		cred.expiresAt - Date.now() <= REFRESH_SKEW_MS &&
		!!cred.refreshToken;
	if (needsRefresh) {
		if (!refreshInFlight) {
			refreshInFlight = (async () => {
				const fresh = await refreshAccessToken(cred);
				if (fresh && writeCred(fresh)) {
					console.log(`${ts()} [Proxy] OAuth token refreshed (new expiry ${new Date(fresh.expiresAt ?? 0).toISOString()})`);
				} else {
					console.error(`${ts()} [Proxy] refresh did not persist — keeping existing token`);
				}
			})().finally(() => { refreshInFlight = null; });
		}
		await refreshInFlight;
		return readCred()?.accessToken ?? cred.accessToken;
	}
	return cred.accessToken;
}

// Back-compat sync reader (startup probe only — does not refresh).
function getOAuthToken(): string | null {
	return readCred()?.accessToken ?? null;
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
// offline parse test) must NOT bind the port, touch the keychain, or exit.
// Match the exact script name, NOT a substring — the offline test file is named
// `credential-proxy-refresh.test.ts`, which contains "credential-proxy" but must
// NOT be treated as the entry point (else importing it tries to bind the port).
const isMain = (process.argv[1] ?? '').endsWith('credential-proxy.ts');

if (isMain) {
	// Verify token exists at startup
	const initToken = getOAuthToken();
	if (!initToken) {
		console.error('No OAuth token found in macOS keychain. Is Claude Code logged in?');
		process.exit(1);
	}
	console.log(`${ts()} [Proxy] OAuth token loaded from keychain (will re-read on each request)`);
}

const upstreamUrl = new URL(UPSTREAM);

const server = createServer((req, res) => {
	const chunks: Buffer[] = [];
	req.on('data', (c) => chunks.push(c));
	req.on('end', async () => {
		const body = Buffer.concat(chunks);

		// Read token fresh from keychain each request, refreshing it first if it
		// is at/near expiry (tokens are also refreshed by active interactive
		// sessions; this self-refresh covers headless nodes where they aren't).
		const oauthToken = await getFreshOAuthToken();
		if (!oauthToken) {
			res.writeHead(502);
			res.end('No OAuth token in keychain');
			return;
		}

		const headers: Record<string, string | number | string[] | undefined> = {
			...(req.headers as Record<string, string>),
			host: upstreamUrl.host,
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

		const upstream = httpsRequest(
			{
				hostname: upstreamUrl.hostname,
				port: 443,
				path: req.url,
				method: req.method,
				headers,
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
					updateQuotaState(quotaHeaders);
				}

				res.writeHead(upRes.statusCode!, upRes.headers);
				upRes.pipe(res);
			},
		);

		upstream.on('error', (err) => {
			console.error(`${ts()} [Proxy] Upstream error:`, err.message);
			if (!res.headersSent) {
				res.writeHead(502);
				res.end('Bad Gateway');
			}
		});

		upstream.write(body);
		upstream.end();
	});
});

if (isMain) {
	server.listen(PORT, '127.0.0.1', () => {
		console.log(`${ts()} [Proxy] Credential proxy → http://localhost:${PORT}`);
		console.log(`${ts()} [Proxy] Upstream: ${UPSTREAM}`);
		console.log(`${ts()} [Proxy] Set ANTHROPIC_BASE_URL=http://localhost:${PORT} to route through proxy`);
	});
}
