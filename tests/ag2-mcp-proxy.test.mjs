// Unit tests for the ag2-mcp-proxy's pure parts: bearer extraction, the
// single-flight token cache, and the retry classification the design turns on
// (evidence §3.4d). Transport is faked — no network.
//
// Run: node tests/ag2-mcp-proxy.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  readBearer, readDescriptor, TokenCache, classify, inbandAuthCode, Failure, createProxy, scrubEnv, mintToken,
  assertSafeUrl, errorEnvelope, envelopeDispatchState, recordRoomAction,
} from '../skills/ag2-space-mcp/ag2-mcp-proxy.mjs';

// mintToken against a fake transport. Bodies are the ones measured on 2026-09-04.
function fakePost(status, body) {
  return async () => ({ status, text: body === undefined ? '' : JSON.stringify(body), headers: new Map() });
}

test('mintToken: 200 → access_token + expires_in, no body sent, no content-type', async () => {
  let sent;
  const post = async (url, body, headers) => { sent = { url, body, headers }; return { status: 200, text: JSON.stringify({ access_token: 'eyJ', token_type: 'Bearer', expires_in: 900, scope: 'mcp:access' }), headers: new Map() }; };
  const r = await mintToken('http://m', 'brr', post);
  assert.equal(r.access_token, 'eyJ');
  assert.equal(sent.body, undefined, 'mint sends NO body');
  assert.equal(sent.headers.authorization, 'Bearer brr');
});

test('mintToken: core-api 401 WITH code → credential failure, not recoverable', async () => {
  await assert.rejects(
    mintToken('http://m', 'brr', fakePost(401, { code: 'UNAUTHORIZED', message: 'agent runtime credential is invalid or inactive', recoverable: false })),
    (e) => e.status === 401 && e.code === 'UNAUTHORIZED' && e.recoverable === false && !e.wrongEndpoint,
  );
});

test('mintToken: 401 WITHOUT code → wrong endpoint, distinct from a bad bearer', async () => {
  // The exact body the local rig-proxy returns when /api/* lands on provision-api.
  await assert.rejects(
    mintToken('http://localhost:9996/api/v1/mcp/agent-access-tokens', 'brr', fakePost(401, { error: 'unauthorized' })),
    (e) => e.wrongEndpoint === true && /not core-api/.test(e.message) && !/bad bearer.*rejected/.test(e.message),
  );
});

test('mintToken: 429 → recoverable with a 60s backoff (no Retry-After header exists)', async () => {
  await assert.rejects(
    mintToken('http://m', 'brr', fakePost(429, { code: 'RATE_LIMITED', recoverable: true })),
    (e) => e.status === 429 && e.recoverable === true && e.retryAfterMs === 60_000,
  );
});

test('mintToken: 2xx without access_token → wrong endpoint, not silently accepted', async () => {
  await assert.rejects(mintToken('http://m', 'brr', fakePost(200, { ok: true })), /not core-api/);
});

function tmp(name, body) {
  const p = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'ag2-mcp-')), name);
  fs.writeFileSync(p, body);
  return p;
}

test('readBearer: takes the secret half after the FIRST pipe, strips quotes', () => {
  const f = tmp('.env', [
    "SSL_CERT_FILE='/x/ca.pem'",
    "AG2_REMOTE_TOKEN_DEV_DAG2_DSPACE='https://dev/relay|other'",
    "AG2_REMOTE_TOKEN='http://localhost:9996/relay|s3cr3t|with|pipes'",
  ].join('\n'));
  assert.equal(readBearer(f, 'AG2_REMOTE_TOKEN'), 's3cr3t|with|pipes');
  assert.equal(readBearer(f, 'AG2_REMOTE_TOKEN_DEV_DAG2_DSPACE'), 'other');
});

test('readBearer: empty value → null (the runbook "unbound" shape)', () => {
  const f = tmp('.env', 'AG2_REMOTE_TOKEN=\n');
  assert.equal(readBearer(f, 'AG2_REMOTE_TOKEN'), null);
});

test('readBearer: exact-key match — a suffixed key never satisfies the unsuffixed one', () => {
  const f = tmp('.env', "AG2_REMOTE_TOKEN_X='u|leak'\n");
  assert.equal(readBearer(f, 'AG2_REMOTE_TOKEN'), null);
});

test('readDescriptor: rejects missing fields and wrong version', () => {
  const ok = tmp('d.json', JSON.stringify({ version: 1, env_file: '/e', env_key: 'K', mint_url: 'http://localhost:8121/m', mcp_url: 'https://c/mcp' }));
  assert.equal(readDescriptor(ok).env_key, 'K');
  const bad = tmp('d.json', JSON.stringify({ version: 1, env_file: '/e' }));
  assert.throws(() => readDescriptor(bad), /missing env_key/);
  const v2 = tmp('d.json', JSON.stringify({ version: 2 }));
  assert.throws(() => readDescriptor(v2), /version 2/);
});

test('TokenCache: single-flight — N concurrent gets produce ONE mint', async () => {
  let mints = 0;
  const cache = new TokenCache({ mint: async () => { mints++; await new Promise((r) => setTimeout(r, 5)); return { access_token: 'tok', expires_in: 900 }; } });
  const results = await Promise.all([cache.get('b'), cache.get('b'), cache.get('b'), cache.get('b')]);
  assert.deepEqual(results, ['tok', 'tok', 'tok', 'tok']);
  assert.equal(mints, 1);
});

test('TokenCache: a short TTL scales the refresh skew instead of minting on every call', async () => {
  let t = 0; let mints = 0;
  const cache = new TokenCache({ now: () => t, mint: async () => { mints++; return { access_token: `tok${mints}`, expires_in: 60 }; } });
  assert.equal(await cache.get('b'), 'tok1');
  t = 30_000; assert.equal(await cache.get('b'), 'tok1', 'still valid at 30s of a 60s token (skew is 15s, not 60s)');
  t = 44_000; assert.equal(await cache.get('b'), 'tok1', 'still valid at 44s');
  t = 46_000; assert.equal(await cache.get('b'), 'tok2', 'refreshed at 46s — 15s before exp');
  assert.equal(mints, 2, 'a 60s TTL must not degrade to mint-per-call');
});

test('TokenCache: follows the server expires_in, 900 is only a fallback', async () => {
  let t = 0; let mints = 0;
  const cache = new TokenCache({ now: () => t, mint: async () => { mints++; return mints === 1 ? { access_token: 'a', expires_in: 300 } : { access_token: 'b' }; } });
  await cache.get('x');
  t = 239_000; assert.equal(await cache.get('x'), 'a', '300s token valid at 239s (skew 60)');
  t = 241_000; assert.equal(await cache.get('x'), 'b', 're-minted at 241s');
  t = 241_000 + 839_000; assert.equal(await cache.get('x'), 'b', 'missing expires_in → 900s fallback still valid at +839s');
  t = 241_000 + 841_000; await cache.get('x'); assert.equal(mints, 3, 'fallback token refreshed at +841s');
});

test('TokenCache: re-mints proactively inside the skew window, not at expiry', async () => {
  let t = 0; let mints = 0;
  const cache = new TokenCache({ now: () => t, mint: async () => { mints++; return { access_token: `tok${mints}`, expires_in: 900 }; } });
  assert.equal(await cache.get('b'), 'tok1');
  t = (900 - 61) * 1000; assert.equal(await cache.get('b'), 'tok1', 'still valid 61s before exp');
  t = (900 - 59) * 1000; assert.equal(await cache.get('b'), 'tok2', 'refreshed 59s before exp');
  assert.equal(mints, 2);
});

// The exact body hosted_facade returned on 2026-09-04 for a revoked/unregistered agent.
const REAL_INBAND_AUTH_FAIL = {
  jsonrpc: '2.0', id: 1,
  result: {
    content: [{
      text: '{"code":"AUTHENTICATION_FAILED","message":"Agent API delegation exchange failed with status 401","recoverable":false,"suggested_action":null,"correlation_id":"a7338830-0000"}',
      type: 'text',
    }],
    isError: true,
  },
};

test('classify: the retry matrix (evidence §3.4d), against the measured wire shape', () => {
  assert.equal(classify(401, null), Failure.AUTH_HTTP);
  assert.equal(classify(200, REAL_INBAND_AUTH_FAIL), Failure.AUTH_INBAND, 'the real double-encoded body must classify as in-band auth');
  assert.equal(classify(200, { result: { isError: true, code: 'AUTHENTICATION_FAILED' } }), Failure.NONE, 'a top-level code is NOT the contract — only content[0].text is');
  assert.equal(classify(200, { result: { isError: true, content: [{ type: 'text', text: '{"code":"NOT_FOUND"}' }] } }), Failure.NONE, 'a non-auth tool error is a normal response, passed through');
  assert.equal(classify(200, { result: { isError: true, content: [{ type: 'text', text: 'plain text, not json' }] } }), Failure.NONE, 'non-JSON text is a normal tool error');
  assert.equal(classify(200, { result: { ok: true } }), Failure.NONE);
  assert.equal(classify(500, null), Failure.AMBIGUOUS);
  assert.equal(classify(502, null), Failure.AMBIGUOUS);
  assert.equal(classify(403, null), Failure.AMBIGUOUS, '403 is NOT in the guaranteed-before-dispatch contract; do not retry');
});

test('classify: EVERY 2xx consults the in-band check, not just 200', () => {
  // A 2xx that is not 200 must never forward a revocation as a successful result.
  for (const s of [200, 201, 202, 204, 299]) {
    assert.equal(classify(s, REAL_INBAND_AUTH_FAIL), Failure.AUTH_INBAND, `status ${s}`);
    assert.equal(classify(s, { result: { ok: true } }), Failure.NONE, `status ${s} clean`);
  }
});

test('TokenCache: a 429 blocks further mints for the backoff window', () => {
  let t = 0; let mints = 0;
  const cache = new TokenCache({ now: () => t, mint: async () => {
    mints++;
    throw Object.assign(new Error('rate limited'), { status: 429, retryAfterMs: 60_000 });
  } });
  return (async () => {
    await assert.rejects(cache.get('b'), /rate limited/);
    assert.equal(mints, 1);
    // second request inside the window must NOT re-mint
    await assert.rejects(cache.get('b'), (e) => e.status === 429 && /backing off/.test(e.message));
    assert.equal(mints, 1, 'a 429 must back off, not re-mint on every request');
    t = 59_000; await assert.rejects(cache.get('b'), /backing off/);
    assert.equal(mints, 1);
    t = 60_001; await assert.rejects(cache.get('b'), /rate limited/);
    assert.equal(mints, 2, 'after the window it may try again');
  })();
});

test('TokenCache: invalidate() resets the skew with the token', async () => {
  let t = 0;
  const cache = new TokenCache({ now: () => t, mint: async () => ({ access_token: 'a', expires_in: 60 }) });
  await cache.get('b');
  assert.equal(cache._skewMs, 15_000, 'short TTL scaled the skew');
  cache.invalidate();
  assert.equal(cache._skewMs, 60_000, 'skew must not survive the token it belonged to');
});

test('assertSafeUrl: https anywhere, http only to loopback', () => {
  assertSafeUrl('mcp_url', 'https://mcp.ag2.space/mcp');
  assertSafeUrl('mint_url', 'http://localhost:9996/api');
  assertSafeUrl('mint_url', 'http://127.0.0.1:8121/api');
  assert.throws(() => assertSafeUrl('mint_url', 'http://evil.example/api'), /must be https/);
  assert.throws(() => assertSafeUrl('mint_url', 'not-a-url'), /not a URL/);
});

test('readDescriptor: rejects a plaintext non-loopback URL', () => {
  const bad = tmp('d.json', JSON.stringify({
    version: 1, env_file: '/e', env_key: 'K',
    mint_url: 'http://attacker.example/mint', mcp_url: 'https://ok/mcp',
  }));
  assert.throws(() => readDescriptor(bad), /mint_url must be https/);
});

test('inbandAuthCode: null when isError is absent or false', () => {
  assert.equal(inbandAuthCode({ result: { code: 'AUTHENTICATION_FAILED' } }), null);
  assert.equal(inbandAuthCode({ result: { isError: false, code: 'AUTHENTICATION_FAILED' } }), null);
});

// Drive createProxy with a fake transport to prove the retry rule end to end.
function fakeProxy(script, { roomActionsFile = null, mint = null } = {}) {
  const env = tmp('.env', "AG2_REMOTE_TOKEN='http://r|bearer1'\n");
  const descriptor = { version: 1, env_file: env, env_key: 'AG2_REMOTE_TOKEN', mint_url: 'http://mint', mcp_url: 'https://mcp' };
  const calls = { mint: 0, post: [] };
  const out = { lines: [], write(s) { this.lines.push(JSON.parse(s)); } };
  const proxy = createProxy({
    descriptor, out, roomActionsFile,
    mint: mint || (async () => { calls.mint++; return { access_token: `tok${calls.mint}`, expires_in: 900 }; }),
    post: async (_url, body, headers) => {
      calls.post.push({ body, auth: headers.authorization, mcpVersion: headers['MCP-Protocol-Version'] });
      const step = script.shift();
      return { status: step.status, text: JSON.stringify(step.body ?? {}), headers: new Map(Object.entries(step.headers ?? {})) };
    },
  });
  return { proxy, calls, out, env };
}

test('proxy: Action arguments, including placement, are forwarded unchanged', async () => {
  const upstream = { jsonrpc: '2.0', id: 41, result: { ok: true } };
  const { proxy, calls } = fakeProxy([{ status: 200, body: upstream }]);
  const rpc = {
    jsonrpc: '2.0', id: 41, method: 'tools/call',
    params: {
      name: 'room.action.execute',
      arguments: {
        room_id: '!room:ag2.space',
        action: 'dynamic.action',
        arguments: {
          reply_to: '$trigger',
          thread_root: '$canonical-root',
          nested: { untouched: ['one', 2, false] },
        },
      },
    },
  };
  await proxy.handle(JSON.stringify(rpc));
  assert.equal(calls.post.length, 1);
  assert.deepEqual(calls.post[0].body, rpc);
});

test('proxy: HTTP 401 → re-mint and retry exactly once, new token on the retry', async () => {
  const { proxy, calls, out } = fakeProxy([
    { status: 401 },
    { status: 200, body: { jsonrpc: '2.0', id: 1, result: { ok: true } } },
  ]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call' }));
  assert.equal(calls.post.length, 2);
  assert.equal(calls.post[0].auth, 'Bearer tok1');
  assert.equal(calls.post[1].auth, 'Bearer tok2', 'retry must carry a freshly minted token');
  assert.deepEqual(out.lines[0].result, { ok: true });
});

test('proxy: HTTP 200 + AUTHENTICATION_FAILED → re-read bearer (rotation) and retry once', async () => {
  const { proxy, calls, out, env } = fakeProxy([
    { status: 200, body: { ...REAL_INBAND_AUTH_FAIL, id: 7 } },
    { status: 200, body: { jsonrpc: '2.0', id: 7, result: { ok: 'after-rotation' } } },
  ]);
  // rotate the bearer on disk between the two calls — the proxy must pick it up
  const origMint = proxy._cache._mint;
  let seenBearers = [];
  proxy._cache._mint = (b) => { seenBearers.push(b); return origMint(b); };
  fs.writeFileSync(env, "AG2_REMOTE_TOKEN='http://r|bearer2'\n");
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'tools/call' }));
  assert.equal(calls.post.length, 2);
  assert.deepEqual(out.lines[0].result, { ok: 'after-rotation' });
  assert.ok(seenBearers.includes('bearer2'), 'the re-mint must use the ROTATED bearer read from disk');
});

test('proxy: a mint failure is MINT, not AMBIGUOUS — nothing was dispatched', async () => {
  const env = tmp('.env', "AG2_REMOTE_TOKEN='http://r|bearer1'\n");
  const descriptor = { version: 1, env_file: env, env_key: 'AG2_REMOTE_TOKEN', mint_url: 'http://localhost:8121/m', mcp_url: 'https://mcp/mcp' };
  const out = { lines: [], write(s) { this.lines.push(JSON.parse(s)); } };
  let posted = 0;
  const proxy = createProxy({
    descriptor, out,
    mint: async () => { throw Object.assign(new Error('mint: http 503'), { status: 503 }); },
    post: async () => { posted++; return { status: 200, text: '{}', headers: new Map() }; },
  });
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call' }));
  assert.equal(posted, 0, 'a failed mint must not dispatch anything');
  assert.match(out.lines[0].error.message, /could not obtain a token \(no call was made\)/);
  assert.equal(/not retried/.test(out.lines[0].error.message), false, 'must not be labelled ambiguous');
});

test('proxy: 5xx is surfaced as a JSON-RPC error and NEVER retried', async () => {
  const { proxy, calls, out } = fakeProxy([{ status: 502 }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 3, method: 'tools/call' }));
  assert.equal(calls.post.length, 1, 'ambiguous failures get exactly one attempt');
  assert.equal(out.lines[0].id, 3);
  assert.match(out.lines[0].error.message, /not retried/);
});

test('proxy: a second 401 after the one retry is surfaced as revoked — never forwards the 401 body, never loops', async () => {
  const { proxy, calls, out } = fakeProxy([{ status: 401, body: { error: 'unauthorized' } }, { status: 401, body: { error: 'unauthorized' } }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/call' }));
  assert.equal(calls.post.length, 2);
  assert.equal(out.lines.length, 1);
  assert.equal(out.lines[0].id, 9);
  assert.ok(out.lines[0].error, 'must be a JSON-RPC error, not the upstream 401 body');
  assert.match(out.lines[0].error.message, /invalid or revoked/);
  assert.equal(out.lines[0].error.message.includes('unauthorized'), false, 'the raw upstream body must not leak through as a response');
});

test('proxy: AUTHENTICATION_FAILED persisting after retry is forwarded as the well-formed tool error it is', async () => {
  const { proxy, calls, out } = fakeProxy([
    { status: 200, body: { ...REAL_INBAND_AUTH_FAIL, id: 11 } },
    { status: 200, body: { ...REAL_INBAND_AUTH_FAIL, id: 11 } },
  ]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 11, method: 'tools/call' }));
  assert.equal(calls.post.length, 2, 'exactly one retry');
  assert.equal(out.lines[0].result.isError, true, 'the core sees the real AUTHENTICATION_FAILED tool error');
});

test('proxy: a 2xx whose body is not JSON-RPC is surfaced, not forwarded as a response', async () => {
  const { proxy, out } = fakeProxy([{ status: 200, body: { totally: 'unrelated' } }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 12, method: 'tools/call' }));
  assert.ok(out.lines[0].error);
  assert.match(out.lines[0].error.message, /without a JSON-RPC body/);
});

test('proxy: a normal tool error (isError, non-auth code) passes through unchanged', async () => {
  const body = { jsonrpc: '2.0', id: 4, result: { isError: true, code: 'NOT_FOUND', content: [] } };
  const { proxy, calls, out } = fakeProxy([{ status: 200, body }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 4, method: 'tools/call' }));
  assert.equal(calls.post.length, 1);
  assert.deepEqual(out.lines[0], body);
});

test('proxy: a forwarded notification (no id) is forwarded and produces no stdout', async () => {
  const { proxy, calls, out } = fakeProxy([{ status: 202 }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/cancelled' }));
  assert.equal(calls.post.length, 1);
  assert.equal(out.lines.length, 0);
});

// Handshake is answered locally so a cold Codex core start never blocks on a
// network mint (initialize overrunning the startup window marks the server failed).
test('proxy: initialize is answered locally — no mint, no upstream, echoes a SUPPORTED client version', async () => {
  const { proxy, calls, out } = fakeProxy([]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-03-26' } }));
  assert.equal(calls.mint, 0, 'must not mint during the handshake');
  assert.equal(calls.post.length, 0, 'must not reach upstream during the handshake');
  assert.equal(out.lines[0].id, 1);
  assert.equal(out.lines[0].result.protocolVersion, '2025-03-26', 'a supported version is echoed');
  assert.ok(out.lines[0].result.capabilities.tools, 'advertises the tools capability');
  assert.equal(out.lines[0].result.serverInfo.name, 'ag2-space');
});

test('proxy: initialize with an UNSUPPORTED version answers the newest we support, never the requested one', async () => {
  const { proxy, out } = fakeProxy([]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2099-01-01' } }));
  assert.equal(out.lines[0].result.protocolVersion, '2025-06-18', 'must not claim a version we do not implement');
});

test('proxy: the negotiated version rides forwarded requests as MCP-Protocol-Version', async () => {
  const { proxy, calls } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 2, result: {} } }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-03-26' } }));
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call' }));
  assert.equal(calls.post.length, 1);
  assert.equal(calls.post[0].mcpVersion, '2025-03-26', 'forwarded request carries the negotiated version');
});

test('proxy: ping is answered locally with an empty result — no mint, no upstream', async () => {
  const { proxy, calls, out } = fakeProxy([]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'ping' }));
  assert.equal(calls.mint, 0);
  assert.equal(calls.post.length, 0);
  assert.deepEqual(out.lines[0], { jsonrpc: '2.0', id: 9, result: {} });
});

test('proxy: notifications/initialized is swallowed — no mint, no upstream, no stdout', async () => {
  const { proxy, calls, out } = fakeProxy([]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }));
  assert.equal(calls.mint, 0);
  assert.equal(calls.post.length, 0);
  assert.equal(out.lines.length, 0);
});

test('proxy: stateless upstream — no session header is ever sent, even if one arrives', async () => {
  const { proxy, calls } = fakeProxy([
    { status: 200, body: { jsonrpc: '2.0', id: 1, result: {} }, headers: { 'mcp-session-id': 'sess-42' } },
    { status: 200, body: { jsonrpc: '2.0', id: 2, result: {} } },
  ]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }));
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call' }));
  assert.equal(calls.post.length, 2);
  for (const c of calls.post) assert.equal(Object.keys(c).includes('session'), false);
});

test('scrubEnv: keeps only the whitelist', () => {
  const env = { HOME: '/h', PATH: '/p', NODE_EXTRA_CA_CERTS: '/ca', AG2_MCP_DESCRIPTOR: '/d', AG2_MCP_LOG: '/l', ANTHROPIC_API_KEY: 'leak', AG2_REMOTE_TOKEN: 'leak' };
  scrubEnv(env);
  assert.deepEqual(Object.keys(env).sort(), ['AG2_MCP_DESCRIPTOR', 'AG2_MCP_LOG', 'HOME', 'NODE_EXTRA_CA_CERTS', 'PATH']);
});

// ---- DevApp contract: ag2-devapp-mcp/v1 ------------------------------------------
// Every DevApp failure is delivered in-band (HTTP 200 + isError), so a non-2xx is
// off-contract. These prove the proxy still preserves `code` and `details` when one
// arrives, never resends, and never calls a proven not_dispatched failure ambiguous.

// The DEVAPP_SLEEPING envelope, from contract fixture c0-devapp-action-read-sleeping.
const SLEEPING = {
  code: 'DEVAPP_SLEEPING',
  message: 'the room app is sleeping',
  recoverable: false,
  correlation_id: 'corr-sleep-1',
  suggested_action: 'report that the app is sleeping; an explicit human wake control is the only way to start it',
  details: {
    source: 'devapp', category: 'DEVAPP_SLEEPING', dispatch_state: 'not_dispatched',
    next_action: 'wait_for_explicit_wake', app_state: 'sleeping',
    room_id: '!fixture-devapp-a:dev.ag2.space', action: 'devapp.app.tasks_list',
  },
};

// ACTION_OUTCOME_UNKNOWN, from c0-devapp-action-execute-outcome-unknown.
const OUTCOME_UNKNOWN = {
  code: 'ACTION_OUTCOME_UNKNOWN',
  message: 'the operation may have taken effect; its outcome is unknown',
  recoverable: false,
  correlation_id: 'corr-unknown-1',
  details: {
    source: 'devapp', category: 'ACTION_OUTCOME_UNKNOWN', dispatch_state: 'dispatched_unknown',
    next_action: 'inspect_operation', operation_id: 'op-tasks-create-7f3a',
    room_id: '!fixture-devapp-a:dev.ag2.space', action: 'devapp.app.tasks_create',
  },
};

const inband = (line) => JSON.parse(line.result.content[0].text);

test('errorEnvelope: bare, nested under error.data, and as the error itself', () => {
  assert.deepEqual(errorEnvelope(SLEEPING), SLEEPING);
  assert.deepEqual(errorEnvelope({ jsonrpc: '2.0', id: 1, error: { code: -32000, data: SLEEPING } }), SLEEPING);
  assert.deepEqual(errorEnvelope({ error: SLEEPING }), SLEEPING);
});

test('errorEnvelope: nothing envelope-shaped → null (no false positives)', () => {
  for (const v of [null, undefined, 'text', 42, [], { message: 'no code' }, { code: 7 }]) {
    assert.equal(errorEnvelope(v), null, JSON.stringify(v));
  }
});

test('envelopeDispatchState: details.dispatch_state is the resend signal, not recoverable', () => {
  assert.equal(envelopeDispatchState(SLEEPING), 'not_dispatched');
  assert.equal(envelopeDispatchState(OUTCOME_UNKNOWN), 'dispatched_unknown');
  assert.equal(envelopeDispatchState({ code: 'X', recoverable: true }), null);
});

test('classify: a not_dispatched envelope is NOT_DISPATCHED, never AMBIGUOUS', () => {
  assert.equal(classify(409, SLEEPING), Failure.NOT_DISPATCHED);
  // dispatched_unknown keeps the conservative may-have-executed reading.
  assert.equal(classify(409, OUTCOME_UNKNOWN), Failure.AMBIGUOUS);
  // No envelope at all → unchanged behaviour.
  assert.equal(classify(503, null), Failure.AMBIGUOUS);
});

test('proxy: an off-contract non-2xx DEVAPP_SLEEPING keeps its code and details, and is not resent', async () => {
  const { proxy, calls, out } = fakeProxy([{ status: 409, body: SLEEPING }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'tools/call' }));
  assert.equal(calls.post.length, 1, 'never resent');
  const [line] = out.lines;
  assert.equal(line.id, 7);
  assert.equal(line.error, undefined, 'delivered in-band, not as a JSON-RPC protocol error');
  assert.equal(line.result.isError, true);
  assert.deepEqual(inband(line), SLEEPING, 'envelope forwarded verbatim');
  assert.equal(inband(line).details.dispatch_state, 'not_dispatched');
});

test('proxy: a non-2xx ACTION_OUTCOME_UNKNOWN preserves operation_id — the inspect handle', async () => {
  const { proxy, calls, out } = fakeProxy([{ status: 409, body: OUTCOME_UNKNOWN }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 8, method: 'tools/call' }));
  assert.equal(calls.post.length, 1, 'an uncertain mutation is never replayed');
  const env = inband(out.lines[0]);
  assert.equal(env.details.operation_id, 'op-tasks-create-7f3a');
  assert.equal(env.details.next_action, 'inspect_operation');
});

test('proxy: an envelope nested in a JSON-RPC error body is recovered, not flattened', async () => {
  const { proxy, out } = fakeProxy([{ status: 503, body: { jsonrpc: '2.0', id: 9, error: { code: -32000, message: 'upstream', data: SLEEPING } } }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/call' }));
  assert.deepEqual(inband(out.lines[0]), SLEEPING);
});

test('proxy: a non-2xx with no envelope still surfaces the opaque not-retried error', async () => {
  const { proxy, calls, out } = fakeProxy([{ status: 502, body: { nothing: 'useful' } }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 10, method: 'tools/call' }));
  assert.equal(calls.post.length, 1);
  assert.match(out.lines[0].error.message, /not retried.*upstream http 502/);
});

test('proxy: the on-contract path — a 200 in-band DEVAPP_SLEEPING passes through untouched', async () => {
  const upstream = { jsonrpc: '2.0', id: 11, result: { isError: true, content: [{ type: 'text', text: JSON.stringify(SLEEPING) }] } };
  const { proxy, calls, out } = fakeProxy([{ status: 200, body: upstream }]);
  await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id: 11, method: 'tools/call' }));
  assert.equal(calls.post.length, 1, 'sleeping is never a retry and never a wake');
  assert.deepEqual(out.lines[0], upstream);
});

test('proxy: the tool half of the name is never rewritten — hooks and the façade key on it', async () => {
  // Wire name stays room.action.execute (Claude Code folds it to room_action_execute
  // for hook matchers); it must cross the proxy byte-identical.
  const { proxy, calls } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 12, result: {} } }]);
  const rpc = {
    jsonrpc: '2.0', id: 12, method: 'tools/call',
    params: { name: 'room.action.execute', arguments: { action: 'devapp.app.tasks_create' } },
  };
  await proxy.handle(JSON.stringify(rpc));
  assert.equal(calls.post[0].body.params.name, 'room.action.execute');
  assert.equal(calls.post[0].body.params.arguments.action, 'devapp.app.tasks_create');
});

// ---- room-action record (contract with sutando's turn_ledger reader, 2026-09-19) ----
function actionsFile() {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'ag2-mcp-ra-')), 'state', 'room-actions.jsonl');
}
function readLines(f) {
  return fs.existsSync(f) ? fs.readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];
}
const EXEC = (id, args = { room_id: '!r:ag2.space', action: 'room.message.send', operation_id: 'op-1', arguments: { body: 'SECRET BODY' } }) =>
  JSON.stringify({ jsonrpc: '2.0', id, method: 'tools/call', params: { name: 'room.action.execute', arguments: args } });

test('room-actions: a successful execute writes exactly one line, stamped at result time, no body', async () => {
  const f = actionsFile();
  const { proxy, out } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 1, result: { content: [] } } }], { roomActionsFile: f });
  const before = Date.now() / 1000;
  await proxy.handle(EXEC(1));
  const lines = readLines(f);
  assert.equal(lines.length, 1);
  assert.deepEqual(Object.keys(lines[0]).sort(), ['action', 'kind', 'operation_id', 'room_id', 'ts']);
  assert.equal(lines[0].kind, 'room-action');
  assert.equal(lines[0].room_id, '!r:ag2.space');
  assert.equal(lines[0].action, 'room.message.send');
  assert.equal(lines[0].operation_id, 'op-1');
  assert.ok(typeof lines[0].ts === 'number' && lines[0].ts >= before && lines[0].ts <= Date.now() / 1000);
  assert.equal(fs.readFileSync(f, 'utf8').includes('SECRET BODY'), false, 'message bodies never recorded');
  assert.equal(out.lines[0].id, 1, 'response still delivered');
});

test('room-actions: a missing operation_id is recorded as null', async () => {
  const f = actionsFile();
  const { proxy } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 1, result: {} } }], { roomActionsFile: f });
  await proxy.handle(EXEC(1, { room_id: '!r:x', action: 'dev.pr.review.publish' }));
  assert.equal(readLines(f)[0].operation_id, null);
  assert.equal(readLines(f)[0].action, 'dev.pr.review.publish');
});

test('room-actions: an isError result writes nothing', async () => {
  const f = actionsFile();
  const { proxy } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 1, result: { isError: true, content: [{ type: 'text', text: '{"code":"FORBIDDEN"}' }] } } }], { roomActionsFile: f });
  await proxy.handle(EXEC(1));
  assert.equal(readLines(f).length, 0);
});

test('room-actions: a JSON-RPC error body on a 2xx writes nothing', async () => {
  const f = actionsFile();
  const { proxy } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 1, error: { code: -32602, message: 'bad params' } } }], { roomActionsFile: f });
  await proxy.handle(EXEC(1));
  assert.equal(readLines(f).length, 0);
});

test('room-actions: AMBIGUOUS (5xx) and NOT_DISPATCHED write nothing', async () => {
  const f = actionsFile();
  const nd = { code: 'DEVAPP_SLEEPING', details: { dispatch_state: 'not_dispatched' } };
  const { proxy } = fakeProxy([{ status: 502 }, { status: 503, body: nd }], { roomActionsFile: f });
  await proxy.handle(EXEC(1));
  await proxy.handle(EXEC(2));
  assert.equal(readLines(f).length, 0);
});

test('room-actions: a MINT failure writes nothing', async () => {
  const f = actionsFile();
  const { proxy, out } = fakeProxy([], { roomActionsFile: f, mint: async () => { throw new Error('mint: http 500'); } });
  await proxy.handle(EXEC(1));
  assert.match(out.lines[0].error.message, /could not obtain a token/);
  assert.equal(readLines(f).length, 0);
});

test('room-actions: persistent in-band auth failure writes nothing', async () => {
  const f = actionsFile();
  const { proxy } = fakeProxy([{ status: 200, body: { ...REAL_INBAND_AUTH_FAIL, id: 1 } }, { status: 200, body: { ...REAL_INBAND_AUTH_FAIL, id: 1 } }], { roomActionsFile: f });
  await proxy.handle(EXEC(1));
  assert.equal(readLines(f).length, 0);
});

test('room-actions: reads (room.action.read, room.inspect) write nothing', async () => {
  const f = actionsFile();
  const ok = (id) => ({ status: 200, body: { jsonrpc: '2.0', id, result: { content: [] } } });
  const { proxy } = fakeProxy([ok(1), ok(2)], { roomActionsFile: f });
  for (const [id, name] of [[1, 'room.action.read'], [2, 'room.inspect']]) {
    await proxy.handle(JSON.stringify({ jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: { room_id: '!r:x' } } }));
  }
  assert.equal(readLines(f).length, 0);
});

test('room-actions: no configured file → nothing written, call unaffected', async () => {
  const { proxy, out } = fakeProxy([{ status: 200, body: { jsonrpc: '2.0', id: 1, result: {} } }]);
  await proxy.handle(EXEC(1));
  assert.deepEqual(out.lines[0], { jsonrpc: '2.0', id: 1, result: {} });
});

test('room-actions: an unwritable path does not break the response', async () => {
  const blocker = tmp('not-a-dir', 'x'); // a FILE where the state dir should be
  const f = path.join(blocker, 'state', 'room-actions.jsonl');
  const body = { jsonrpc: '2.0', id: 1, result: { content: [] } };
  const { proxy, out } = fakeProxy([{ status: 200, body }], { roomActionsFile: f });
  await proxy.handle(EXEC(1));
  assert.deepEqual(out.lines, [body]);
  assert.equal(recordRoomAction(f, { room_id: '!r:x', action: 'a' }), false, 'reports failure, never throws');
});

test('room-actions: above 256 KiB the file rotates to .1 and the new line starts a fresh file', () => {
  const f = actionsFile();
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(`${f}.1`, 'stale\n');
  const big = '{"ts":1,"kind":"room-action"}\n'.repeat(Math.ceil((256 * 1024 + 1) / 29));
  fs.writeFileSync(f, big);
  assert.ok(recordRoomAction(f, { room_id: '!r:x', action: 'a' }, 5_000));
  assert.equal(fs.readFileSync(`${f}.1`, 'utf8'), big, 'old .1 overwritten by the full file');
  assert.deepEqual(readLines(f), [{ ts: 5, kind: 'room-action', room_id: '!r:x', action: 'a', operation_id: null }]);
});

test('room-actions: at or under the cap, no rotation', () => {
  const f = actionsFile();
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, 'x'.repeat(256 * 1024 - 1) + '\n');
  assert.ok(recordRoomAction(f, { room_id: '!r:x', action: 'a' }));
  assert.equal(fs.existsSync(`${f}.1`), false);
});

test('room-actions: a line over 4 KiB is not written', () => {
  const f = actionsFile();
  assert.equal(recordRoomAction(f, { room_id: 'r'.repeat(5000), action: 'a' }), false);
  assert.equal(readLines(f).length, 0);
});

test('room-actions: non-string arguments are recorded as null, never as objects', () => {
  const f = actionsFile();
  assert.ok(recordRoomAction(f, { room_id: { nested: 'x' }, action: 7, operation_id: '' }));
  assert.deepEqual(readLines(f)[0], { ts: readLines(f)[0].ts, kind: 'room-action', room_id: null, action: null, operation_id: null });
});

test('scrubEnv: keeps AG2_MCP_ROOM_ACTIONS (Codex passes no inherited env)', () => {
  const env = { AG2_MCP_ROOM_ACTIONS: '/w/state/room-actions.jsonl', DROP_ME: '1' };
  scrubEnv(env);
  assert.deepEqual(env, { AG2_MCP_ROOM_ACTIONS: '/w/state/room-actions.jsonl' });
});
