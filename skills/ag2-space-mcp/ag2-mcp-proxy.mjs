#!/usr/bin/env node
// ag2-mcp-proxy — stdio MCP server fronting the hosted AG2 Space MCP endpoint.
// Holds a 15-min access token in memory, minted from the registry bearer in the
// lane .env (`<relay_url>|<secret>`); the bearer is never sent to /mcp.
// Re-mints on expiry or auth rejection. Descriptor + CA paths come via the config
// entry's `env` (Codex passes no inherited env). Exits on stdin EOF or ppid==1.
// Design: docs/mcp-credential-bridge-design.html.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { createInterface } from 'node:readline';

// ---- constants --------------------------------------------------------------
const DESCRIPTOR_ENV = 'AG2_MCP_DESCRIPTOR';
const LOG_FILE_ENV = 'AG2_MCP_LOG'; // append-only mint/lifecycle log; the host's stderr capture stops after connect
// Append-only record of successful room.action.execute calls: <workspace>/state/room-actions.jsonl.
// Sutando's turn gate reads it; the proxy only records facts. Contract agreed with sutando 2026-09-19.
const ROOM_ACTIONS_ENV = 'AG2_MCP_ROOM_ACTIONS';
const ROOM_ACTIONS_MAX_BYTES = 256 * 1024; // writer-owned cap: rotate to `.1` above this
const ROOM_ACTION_LINE_MAX = 4096;
const ORPHAN_POLL_MS = 2000;
const REFRESH_SKEW_S = 60; // re-mint this many seconds before `exp`, capped at ttl/4 for short TTLs
const MINT_TIMEOUT_MS = 15_000;
const FORWARD_TIMEOUT_MS = 120_000;
// Versions the proxy will speak, newest first. MCP negotiation: echo the client's
// version only when it is one of these, else answer with the newest we support.
// The negotiated version then rides forwarded requests as MCP-Protocol-Version so
// the stateless /mcp upstream uses the same one instead of its own default.
const SUPPORTED_PROTOCOL_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05'];
const LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0];
const PROXY_VERSION = '1.0.0';
const KEEP_ENV = new Set([
  'HOME', 'PATH', 'TMPDIR', 'LANG', 'USER', 'LOGNAME', 'SHELL', 'TERM',
  'NODE_EXTRA_CA_CERTS', DESCRIPTOR_ENV, LOG_FILE_ENV, ROOM_ACTIONS_ENV,
]);

// ---- logging: stderr + optional file, never a secret ----------------------
const LOG_FILE = process.env[LOG_FILE_ENV] || null;
function log(msg) {
  const line = `ag2-mcp-proxy: ${msg}\n`;
  process.stderr.write(line);
  if (LOG_FILE) { try { fs.appendFileSync(LOG_FILE, `${new Date().toISOString()} pid=${process.pid} ${line}`); } catch { /* best-effort */ } }
}
export function fingerprint(secret) {
  return createHash('sha256').update(secret).digest('hex').slice(0, 8);
}

// ---- room-action record ---------------------------------------------------------
// One line per successful room.action.execute, stamped when the result came back
// (the gate's window is measured from the reply, not the request). One
// appendFileSync (O_APPEND) per line keeps concurrent proxies — Codex runs one per
// subagent — from interleaving. Rotation races between proxies may drop a `.1`;
// accepted, the reader only needs the last few seconds. Best-effort: never throws.
export function recordRoomAction(file, args, nowMs = Date.now()) {
  if (!file) return false;
  const a = args && typeof args === 'object' ? args : {};
  const str = (v) => (typeof v === 'string' && v ? v : null);
  const line = JSON.stringify({
    ts: nowMs / 1000,
    kind: 'room-action',
    room_id: str(a.room_id),
    action: str(a.action),
    operation_id: str(a.operation_id),
  }) + '\n';
  if (Buffer.byteLength(line) > ROOM_ACTION_LINE_MAX) { log('room-action line over 4 KiB — not recorded'); return false; }
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    try {
      if (fs.statSync(file).size > ROOM_ACTIONS_MAX_BYTES) fs.renameSync(file, `${file}.1`);
    } catch (e) { if (e.code !== 'ENOENT') throw e; }
    fs.appendFileSync(file, line);
    return true;
  } catch (e) {
    log(`room-action record failed (call unaffected): ${e.code || e.message}`);
    return false;
  }
}

// ---- env scrub ---------------------------------------------------------------
// Claude Code passes the core's env through; keep only what is used. Affects
// process.env and children, not the kernel's exec-time environ (`ps eww`).
export function scrubEnv(env = process.env) {
  for (const k of Object.keys(env)) if (!KEEP_ENV.has(k)) delete env[k];
}

// ---- descriptor ---------------------------------------------------------------
// Written by agent_mcp.rs. Non-secret. Lane resolution is host-side only.
export function readDescriptor(path) {
  const raw = fs.readFileSync(path, 'utf8');
  const d = JSON.parse(raw);
  if (d.version !== 1) throw new Error(`descriptor version ${d.version} unsupported`);
  for (const k of ['env_file', 'env_key', 'mint_url', 'mcp_url']) {
    if (typeof d[k] !== 'string' || !d[k]) throw new Error(`descriptor missing ${k}`);
  }
  // The bearer is sent to mint_url, so a rewritten descriptor could redirect it.
  // Require TLS, or plaintext only to loopback (local rigs).
  for (const k of ['mint_url', 'mcp_url']) assertSafeUrl(k, d[k]);
  return d;
}

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);
export function assertSafeUrl(field, value) {
  let u;
  try { u = new URL(value); } catch { throw new Error(`descriptor ${field} is not a URL`); }
  if (u.protocol === 'https:') return;
  if (u.protocol === 'http:' && LOOPBACK.has(u.hostname)) return;
  throw new Error(`descriptor ${field} must be https (or http to loopback): ${u.protocol}//${u.hostname}`);
}

// ---- bearer ---------------------------------------------------------------------
// Lane .env is shell-sourced; values are quoted. Exact-key match, strip quotes,
// take the secret after the FIRST `|` (URL half is the relay, unused here).
export function readBearer(envFile, envKey) {
  const text = fs.readFileSync(envFile, 'utf8');
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq === -1 || t.slice(0, eq) !== envKey) continue;
    let v = t.slice(eq + 1).trim();
    if ((v.startsWith("'") && v.endsWith("'")) || (v.startsWith('"') && v.endsWith('"'))) {
      v = v.slice(1, -1);
    }
    if (!v) return null;
    const pipe = v.indexOf('|');
    return pipe === -1 ? v : v.slice(pipe + 1);
  }
  return null;
}

// ---- token cache + single-flight mint ------------------------------------------
export class TokenCache {
  constructor({ mint, now = () => Date.now() }) {
    this._mint = mint; // async (bearer) => { access_token, expires_in }
    this._now = now;
    this._token = null;
    this._expMs = 0;
    this._skewMs = REFRESH_SKEW_S * 1000;
    this._blockedUntilMs = 0;
    this._inflight = null;
  }
  get valid() {
    return !!this._token && this._now() < this._expMs - this._skewMs;
  }
  invalidate() { this._token = null; this._expMs = 0; this._skewMs = REFRESH_SKEW_S * 1000; }
  // Single-flight: concurrent callers share one mint.
  async get(bearer) {
    if (this.valid) return this._token;
    // Honour a rate-limit penalty rather than re-minting per request: the mint
    // endpoint caps per bearer and sends no Retry-After.
    if (this._now() < this._blockedUntilMs) {
      const s = Math.ceil((this._blockedUntilMs - this._now()) / 1000);
      throw Object.assign(new Error(`mint: rate limited, backing off ${s}s`), { status: 429, recoverable: true });
    }
    if (!this._inflight) {
      this._inflight = this._mint(bearer)
        .catch((e) => {
          if (e && e.retryAfterMs) this._blockedUntilMs = this._now() + e.retryAfterMs;
          throw e;
        })
        .then((r) => {
          if (!r || typeof r.access_token !== 'string') throw new Error('mint: malformed response');
          const ttl = Number(r.expires_in) > 0 ? Number(r.expires_in) : 900;
          this._token = r.access_token;
          this._expMs = this._now() + ttl * 1000;
          this._skewMs = Math.min(REFRESH_SKEW_S, Math.floor(ttl / 4)) * 1000;
          log(`minted token fp=${fingerprint(this._token)} ttl=${ttl}s`);
          return this._token;
        })
        .finally(() => { this._inflight = null; });
    }
    return this._inflight;
  }
}

// ---- failure classification -------------------------------------------------------
// Retry once after re-mint: AUTH_HTTP (401) and AUTH_INBAND (200 + isError,
// code AUTHENTICATION_FAILED) — both precede tool dispatch server-side, the
// DevApp path included (guarantees.auth_failure_is_always_pre_dispatch).
// Anything else is AMBIGUOUS (may have executed): surface, never retry.
// MINT is terminal but NOT ambiguous: no request was dispatched, so nothing can
// have half-happened. Ambiguity belongs only to a failure after dispatch.
// NOT_DISPATCHED: a non-2xx whose envelope proves nothing ran; never retried.
export const Failure = Object.freeze({ NONE: 'none', AUTH_HTTP: 'auth-http', AUTH_INBAND: 'auth-inband', MINT: 'mint', AMBIGUOUS: 'ambiguous', NOT_DISPATCHED: 'not-dispatched' });

// ---- capability-api error envelopes -------------------------------------------------
// DevApp failures arrive in-band (200 + isError), so a non-2xx is off-contract;
// recover its envelope anyway (bare or under JSON-RPC error.data) to keep code/details.
export function errorEnvelope(body) {
  if (!body || typeof body !== 'object') return null;
  const shaped = (v) => (v && typeof v === 'object' && !Array.isArray(v) && typeof v.code === 'string' ? v : null);
  return shaped(body) || shaped(body.error && body.error.data) || shaped(body.error);
}

// `details.dispatch_state` is the ONLY resend signal in the contract
// (error-catalog.json retry_decision); `recoverable` is not one.
export function envelopeDispatchState(envelope) {
  const d = envelope && envelope.details;
  return d && typeof d.dispatch_state === 'string' ? d.dispatch_state : null;
}

// The in-band error object is a JSON string in result.content[0].text (no structuredContent).
export function inbandAuthCode(rpc) {
  const r = rpc && rpc.result;
  if (!r || r.isError !== true) return null;
  const c0 = Array.isArray(r.content) && r.content[0];
  if (!c0 || typeof c0.text !== 'string') return null;
  try { const j = JSON.parse(c0.text); return typeof j.code === 'string' ? j.code : null; }
  catch { return null; }
}
export function classify(status, rpc) {
  if (status === 401) return Failure.AUTH_HTTP;
  // Same 2xx range as the success branch: a non-200 success must not skip the
  // in-band check, or a revocation would be forwarded as a successful result.
  const ok2xx = status >= 200 && status < 300;
  if (ok2xx && inbandAuthCode(rpc) === 'AUTHENTICATION_FAILED') return Failure.AUTH_INBAND;
  if (ok2xx) return Failure.NONE;
  // An envelope saying not_dispatched is evidence, not a guess: never call it
  // ambiguous. Every other non-2xx keeps the conservative may-have-executed read.
  if (envelopeDispatchState(errorEnvelope(rpc)) === 'not_dispatched') return Failure.NOT_DISPATCHED;
  return Failure.AMBIGUOUS;
}

// ---- HTTP -----------------------------------------------------------------------------
async function postJson(url, body, headers, timeoutMs) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const init = { method: 'POST', headers: { accept: 'application/json', ...headers }, signal: ac.signal };
    if (body !== undefined) { init.headers['content-type'] = 'application/json'; init.body = JSON.stringify(body); }
    const res = await fetch(url, init);
    const text = await res.text();
    return { status: res.status, text, headers: res.headers };
  } finally {
    clearTimeout(t);
  }
}

// POST, Bearer <registry>, no body → {access_token, expires_in}. 401 with `code`
// = bearer revoked; 401 WITHOUT `code` = wrong mint_url (another service).
// 429 RATE_LIMITED: 30/60s per agent, no Retry-After header.
const RATE_LIMIT_BACKOFF_MS = 60_000;
export async function mintToken(mintUrl, bearer, post = postJson) {
  const { status, text } = await post(mintUrl, undefined, { authorization: `Bearer ${bearer}` }, MINT_TIMEOUT_MS);
  let body = null;
  try { body = JSON.parse(text); } catch { /* handled below */ }
  const code = body && typeof body.code === 'string' ? body.code : null;
  if (status === 401) {
    if (!code) throw Object.assign(new Error(`mint: 401 without a \`code\` field — ${mintUrl} is not core-api's mint endpoint (wrong mint_url), not a bad bearer`), { status, wrongEndpoint: true });
    throw Object.assign(new Error(`mint: bearer rejected (${code}, not recoverable)`), { status, code, recoverable: false });
  }
  if (status === 429) throw Object.assign(new Error(`mint: rate limited (${code ?? 'RATE_LIMITED'}, retry after 60s)`), { status, code, recoverable: true, retryAfterMs: RATE_LIMIT_BACKOFF_MS });
  if (status < 200 || status >= 300) throw Object.assign(new Error(`mint: http ${status}`), { status });
  if (!body || typeof body.access_token !== 'string') throw new Error('mint: 2xx without access_token — not core-api\'s mint endpoint');
  return body;
}

// Upstream is stateless_http: JSON responses only, no session id, no server→client
// traffic (tools.listChanged=false, no sampling/elicitation). No GET stream.
function parseRpcBody(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}

// ---- the proxy ------------------------------------------------------------------------
export function createProxy({ descriptor, mint = mintToken, post = postJson, out = process.stdout, now, roomActionsFile = null }) {
  const cache = new TokenCache({ mint: (b) => mint(descriptor.mint_url, b), now });
  // Set at initialize (one client per process), then sent upstream on every forward.
  let negotiatedProtocol = LATEST_PROTOCOL_VERSION;

  function write(msg) { out.write(JSON.stringify(msg) + '\n'); }
  function rpcError(id, code, message) { write({ jsonrpc: '2.0', id, error: { code, message } }); }
  // In-band MCP tool error: the delivery form the contract freezes for every
  // DevApp failure. JSON-RPC `error` stays reserved for unknown tool / bad params.
  function toolError(id, envelope) {
    write({ jsonrpc: '2.0', id, result: { isError: true, content: [{ type: 'text', text: JSON.stringify(envelope) }] } });
  }

  async function forwardOnce(rpc) {
    const bearer = readBearer(descriptor.env_file, descriptor.env_key);
    if (!bearer) throw Object.assign(new Error('no registry bearer in lane env'), { fatal: true });
    let token;
    try { token = await cache.get(bearer); }
    catch (e) { throw Object.assign(e, { phase: 'mint' }); }
    const res = await post(
      descriptor.mcp_url,
      rpc,
      { authorization: `Bearer ${token}`, 'MCP-Protocol-Version': negotiatedProtocol },
      FORWARD_TIMEOUT_MS,
    );
    return { status: res.status, body: parseRpcBody(res.text) };
  }

  // One retry, only for AUTH_HTTP / AUTH_INBAND.
  const failed = (e) => ({ kind: e.phase === 'mint' ? Failure.MINT : Failure.AMBIGUOUS, error: e });

  async function forward(rpc) {
    let r;
    try { r = await forwardOnce(rpc); }
    catch (e) { if (e.fatal) throw e; return failed(e); }
    let kind = classify(r.status, r.body);
    if (kind === Failure.AUTH_HTTP || kind === Failure.AUTH_INBAND) {
      log(`auth rejected (${kind}) — re-reading bearer, re-minting, retrying once`);
      cache.invalidate();
      try { r = await forwardOnce(rpc); }
      catch (e) { if (e.fatal) throw e; return failed(e); }
      kind = classify(r.status, r.body);
    }
    return { kind, status: r.status, body: r.body };
  }

  async function handle(line) {
    let rpc;
    // MCP stdio is one JSON per line. Log a malformed line: after connect the
    // host stops capturing stderr, so AG2_MCP_LOG is the only forensic surface.
    try { rpc = JSON.parse(line); } catch { log(`dropped unparseable stdin line (${line.length} bytes)`); return; }
    const isNotification = rpc.id === undefined || rpc.id === null;
    // Answer the handshake locally: a cold Codex core start marks a server failed
    // if `initialize` overruns its startup window, and forwarding it (or the
    // following notifications/initialized) would block that window on a network
    // mint. Mint lazily on the first real call instead. The /mcp endpoint is
    // stateless per-request, so it needs no initialize of its own.
    if (rpc.method === 'initialize') {
      // Echo the client's version only when we support it, else the newest we do
      // (MCP negotiation rule). Remember it for the MCP-Protocol-Version header.
      const requested = rpc.params?.protocolVersion;
      negotiatedProtocol = SUPPORTED_PROTOCOL_VERSIONS.includes(requested)
        ? requested
        : LATEST_PROTOCOL_VERSION;
      write({ jsonrpc: '2.0', id: rpc.id, result: {
        protocolVersion: negotiatedProtocol,
        capabilities: { tools: {} },
        serverInfo: { name: 'ag2-space', version: PROXY_VERSION },
      } });
      return;
    }
    if (rpc.method === 'ping') { write({ jsonrpc: '2.0', id: rpc.id, result: {} }); return; }
    if (rpc.method === 'notifications/initialized') return; // swallow: no body, no mint
    let res;
    try { res = await forward(rpc); }
    catch (e) { log(`fatal: ${e.message}`); if (!isNotification) rpcError(rpc.id, -32000, e.message); return; }
    if (isNotification) return; // 202 / no body expected
    if (res.kind === Failure.MINT) {
      // Nothing was dispatched — terminal for this call, but no side effect.
      log(`could not obtain a token: ${res.error.message}`);
      rpcError(rpc.id, -32000, `AG2 MCP could not obtain a token (no call was made): ${res.error.message}`);
      return;
    }
    if (res.kind === Failure.NOT_DISPATCHED || res.kind === Failure.AMBIGUOUS) {
      // Off-contract non-2xx carrying a capability-api envelope: forward code and
      // details verbatim rather than the opaque status. Still never retried — this
      // only stops DEVAPP_* / operation_id / correlation_id being thrown away.
      const envelope = errorEnvelope(res.body);
      if (envelope) {
        const state = envelopeDispatchState(envelope) || 'unspecified';
        log(`upstream http ${res.status} carried ${envelope.code} (dispatch_state=${state}) — forwarded verbatim, not retried`);
        toolError(rpc.id, envelope);
        return;
      }
      const why = res.error ? res.error.message : `upstream http ${res.status}`;
      log(`ambiguous failure, surfaced without retry: ${why}`);
      rpcError(rpc.id, -32000, `AG2 MCP upstream failure (not retried): ${why}`);
      return;
    }
    // 401 persisting after the retry: revoked or rotated-and-refused. Never forward
    // the 401 body as a response.
    if (res.kind === Failure.AUTH_HTTP) {
      log('auth still rejected after re-mint — surfacing as revoked/invalid');
      rpcError(rpc.id, -32000, 'AG2 MCP authentication failed after re-mint: the agent credential is invalid or revoked');
      return;
    }
    // AUTH_INBAND after retry is a well-formed tool error: forward as-is.
    if (res.body && res.body.jsonrpc === '2.0') {
      write(res.body);
      // After the response is written, so recording can never delay or fail the call.
      if (res.kind === Failure.NONE && rpc.method === 'tools/call' && rpc.params?.name === 'room.action.execute'
        && res.body.result && res.body.result.isError !== true) {
        recordRoomAction(roomActionsFile, rpc.params.arguments);
      }
      return;
    }
    rpcError(rpc.id, -32000, `AG2 MCP upstream returned http ${res.status} without a JSON-RPC body`);
  }

  return { handle, _cache: cache };
}

// ---- main -----------------------------------------------------------------------------
function main() {
  const descPath = process.env[DESCRIPTOR_ENV];
  const roomActionsFile = process.env[ROOM_ACTIONS_ENV] || null;
  scrubEnv();
  if (!descPath) { log(`${DESCRIPTOR_ENV} not set — refusing to start`); process.exit(2); }
  let descriptor;
  try { descriptor = readDescriptor(descPath); }
  catch (e) { log(`descriptor unreadable: ${e.message}`); process.exit(2); }
  log(`start lane=${descriptor.lane ?? 'primary'} mcp=${descriptor.mcp_url} ppid=${process.ppid}`);

  const proxy = createProxy({ descriptor, roomActionsFile });

  // Watchdog: parent EOF, or parent gone (ppid==1).
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', (line) => { proxy.handle(line).catch((e) => log(`handler error: ${e.message}`)); });
  rl.on('close', () => { log('stdin closed — exiting'); process.exit(0); });
  setInterval(() => { if (process.ppid === 1) { log('orphaned (ppid=1) — exiting'); process.exit(0); } }, ORPHAN_POLL_MS).unref();
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(sig, () => { log(`${sig} — exiting`); process.exit(0); });
}

// Path compare, not URL compare: import.meta.url percent-encodes spaces
// ("Application Support") so a string match against argv[1] never fires.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
