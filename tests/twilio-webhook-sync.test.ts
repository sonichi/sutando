/**
 * The phone server's startup webhook sync (TWILIO_AUTO_WEBHOOK=1) against a
 * fake fetch: the same two calls as `twilio-setup.py set-webhook`, every call
 * under a deadline, and every failure — a Twilio API that never answers
 * included — a logged skip that returns instead of holding the start.
 *
 * Runs under `tsx --test` (npm test); needs no build and no network.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { syncTwilioWebhook, TWILIO_SYNC_TIMEOUT_MS } from '../skills/phone-conversation/scripts/twilio-webhook-sync.js';

const CREDS = { sid: 'ACtest', token: 'secret', number: '+14155550100' };
const BASE = 'https://b.ngrok-free.app';
const HERE = { sid: 'PN1', voice_url: `${BASE}/twilio/connect`, status_callback: `${BASE}/twilio/status` };
const ELSEWHERE = { sid: 'PN1', voice_url: 'https://a.ngrok-free.app/twilio/connect', status_callback: 'https://a.ngrok-free.app/twilio/status' };

type Call = { url: string; init: RequestInit };
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

function fetchWith(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
	const calls: Call[] = [];
	const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
		const url = String(input);
		calls.push({ url, init: init ?? {} });
		return handler(url, init ?? {});
	}) as typeof fetch;
	return { fetchImpl, calls };
}

function sink() {
	const out: string[] = [];
	const err: string[] = [];
	return { out, err, log: (l: string) => out.push(l), error: (l: string) => err.push(l) };
}

test('a number already pointing here is left alone', async () => {
	const { fetchImpl, calls } = fetchWith(() => json(200, { incoming_phone_numbers: [HERE] }));
	const s = sink();
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl, log: s.log, error: s.error }), 'unchanged');
	assert.equal(calls.length, 1);
	assert.match(calls[0].url, /\/Accounts\/ACtest\/IncomingPhoneNumbers\.json\?PhoneNumber=%2B14155550100$/);
	assert.equal(calls[0].init.headers && (calls[0].init.headers as Record<string, string>).Authorization,
		`Basic ${Buffer.from('ACtest:secret').toString('base64')}`);
	assert.deepEqual(s.out, ['[Twilio] webhook already points here']);
	assert.deepEqual(s.err, []);
});

test('a drifted number is re-pointed with the same form as set-webhook', async () => {
	const { fetchImpl, calls } = fetchWith((url) =>
		url.endsWith('/IncomingPhoneNumbers/PN1.json') ? json(200, HERE) : json(200, { incoming_phone_numbers: [ELSEWHERE] }));
	const s = sink();
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl, log: s.log, error: s.error }), 'updated');
	assert.equal(calls.length, 2);
	assert.equal(calls[1].init.method, 'POST');
	const form = calls[1].init.body as URLSearchParams;
	assert.deepEqual(Object.fromEntries(form.entries()), {
		VoiceUrl: `${BASE}/twilio/connect`, VoiceMethod: 'POST',
		StatusCallback: `${BASE}/twilio/status`, StatusCallbackMethod: 'POST',
	});
	assert.deepEqual(s.out, [`[Twilio] webhook now ${BASE}/twilio/connect`]);
});

test('every call to api.twilio.com carries a deadline, 10 s by default', async () => {
	const { fetchImpl, calls } = fetchWith((url) =>
		url.endsWith('/PN1.json') ? json(200, HERE) : json(200, { incoming_phone_numbers: [ELSEWHERE] }));
	await syncTwilioWebhook(CREDS, BASE, { fetchImpl });
	assert.equal(calls.length, 2);
	for (const c of calls) assert.ok(c.init.signal instanceof AbortSignal, `${c.url} has no AbortSignal`);
	assert.equal(TWILIO_SYNC_TIMEOUT_MS, 10_000);
});

test('a Twilio API that never answers is a logged skip at the deadline, not a stalled startup', async () => {
	const hanging = ((_: unknown, init?: RequestInit) => new Promise<Response>((_, reject) => {
		const sig = init?.signal;
		if (!sig) { reject(new Error('fetch called without a deadline')); return; }
		sig.addEventListener('abort', () => reject(sig.reason), { once: true });
	})) as typeof fetch;
	const s = sink();
	const t0 = Date.now();
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl: hanging, timeoutMs: 40, log: s.log, error: s.error }), 'skipped');
	assert.ok(Date.now() - t0 < 2_000, 'the sync did not return at its deadline');
	assert.equal(s.err.length, 1);
	assert.match(s.err[0], /webhook sync skipped: api\.twilio\.com did not answer within 40 ms/);
	assert.match(s.err[0], /twilio-setup\.py set-webhook/);
	assert.deepEqual(s.out, []);
});

test('HTTP failures and an unowned number are logged skips', async () => {
	let s = sink();
	let f = fetchWith(() => json(401, { message: 'Authenticate' }));
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl: f.fetchImpl, log: s.log, error: s.error }), 'skipped');
	assert.deepEqual(s.err, ['[Twilio] webhook sync: list failed HTTP 401']);

	s = sink();
	f = fetchWith(() => json(200, { incoming_phone_numbers: [] }));
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl: f.fetchImpl, log: s.log, error: s.error }), 'skipped');
	assert.match(s.err[0], /not owned by this account/);

	s = sink();
	f = fetchWith((url) => url.endsWith('/PN1.json')
		? new Response('nope', { status: 400 })
		: json(200, { incoming_phone_numbers: [ELSEWHERE] }));
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl: f.fetchImpl, log: s.log, error: s.error }), 'skipped');
	assert.deepEqual(s.err, ['[Twilio] webhook sync: update failed HTTP 400: nope']);
});

test('a network error is a logged skip, never a throw', async () => {
	const s = sink();
	const failing = (async () => { throw new Error('ECONNRESET'); }) as typeof fetch;
	assert.equal(await syncTwilioWebhook(CREDS, BASE, { fetchImpl: failing, log: s.log, error: s.error }), 'skipped');
	assert.deepEqual(s.err, ['[Twilio] webhook sync failed: ECONNRESET']);
});
