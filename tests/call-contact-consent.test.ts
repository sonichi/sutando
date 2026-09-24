import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/**
 * The voice/phone `call_contact` inline tool drives the native macOS Contacts app
 * (a macOS Automation prompt on the owner's screen), so it sits behind the same
 * consent as the macos-tools scripts and the morning briefing:
 *
 *   - no host opt-in → no `open`, no osascript, an answer telling the model what
 *     the owner must do once;
 *   - SUTANDO_ALLOW_NATIVE_PIM=1 or <workspace>/state/native-pim-consent → the
 *     lookup runs;
 *   - a stored macOS denial (<workspace>/state/contacts-automation-denied) is
 *     final: no subprocess, no retry; a fresh -1743 writes that marker.
 *
 * All subprocess and fetch calls are injected — nothing real runs here, and the
 * tool is built with a macOS stub so the gate is exercised on every CI platform.
 * Run: npx tsx --test tests/call-contact-consent.test.ts
 */

import { makeCallContactTool } from '../src/meeting-tools.js';
import { checkNativePimConsent, recordNativePimDenial } from '../src/native-pim-consent.js';

type Result = Record<string, unknown>;

const OSA_OUTPUT = Buffer.from('Mary Smith|||+15551234567,\n');

function fakeExec(calls: string[][], result: Buffer | Error = OSA_OUTPUT) {
	return ((file: string, args: string[]) => {
		calls.push([file, ...args]);
		if (result instanceof Error) throw result;
		return result;
	}) as unknown as typeof import('node:child_process').execFileSync;
}

function fakeFetch(calls: unknown[]) {
	return (async (url: unknown, init?: unknown) => {
		calls.push({ url, init });
		return { ok: true, statusText: 'OK', json: async () => ({ callSid: 'CA1', status: 'queued' }) } as Response;
	}) as unknown as typeof fetch;
}

describe('call_contact native Contacts consent gate', () => {
	let ws: string;
	beforeEach(() => {
		ws = mkdtempSync(join(tmpdir(), 'call-contact-'));
		mkdirSync(join(ws, 'state'), { recursive: true });
	});
	afterEach(() => rmSync(ws, { recursive: true, force: true }));

	it('without consent: no subprocess, no call, and the answer names the owner action', async () => {
		const exec: string[][] = [];
		const fetched: unknown[] = [];
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec), fetch: fakeFetch(fetched), isMacOS: () => true, env: {}, workspace: ws });
		const res = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.equal(exec.length, 0, 'no open/osascript without the owner opt-in');
		assert.equal(fetched.length, 0);
		assert.equal(res.status, 'no-consent');
		assert.equal(res.contactsSearched, false);
		assert.match(String(res.instruction), /native_pim_consent\.py grant/);
		assert.match(String(res.instruction), /SUTANDO_ALLOW_NATIVE_PIM=1/);
	});

	it('SUTANDO_ALLOW_NATIVE_PIM=1 in the server env lets the lookup and the call proceed', async () => {
		const exec: string[][] = [];
		const fetched: unknown[] = [];
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec), fetch: fakeFetch(fetched), isMacOS: () => true, env: { SUTANDO_ALLOW_NATIVE_PIM: '1' }, workspace: ws });
		const res = (await tool.execute({ name: 'Mary Smith', message: 'hi' }, {} as never)) as Result;
		assert.equal(res.status, 'calling');
		assert.equal(res.contact, 'Mary Smith');
		assert.equal(exec.length, 1);
		assert.equal(exec[0][0], '/usr/bin/osascript');
		assert.ok(!exec.some(c => c[0] === 'open'), 'the tool never launches Contacts.app itself');
		assert.equal(fetched.length, 1);
	});

	it('the persisted consent marker counts like the env var', async () => {
		writeFileSync(join(ws, 'state', 'native-pim-consent'), 'owner');
		const exec: string[][] = [];
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec), fetch: fakeFetch([]), isMacOS: () => true, env: {}, workspace: ws });
		const res = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.equal(res.status, 'calling');
		assert.equal(exec.length, 1);
	});

	it('a look-alike env value is not consent', async () => {
		const exec: string[][] = [];
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec), fetch: fakeFetch([]), isMacOS: () => true, env: { SUTANDO_ALLOW_NATIVE_PIM: '10' }, workspace: ws });
		const res = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.equal(res.status, 'no-consent');
		assert.equal(exec.length, 0);
	});

	it('a stored macOS denial is final: no subprocess even with consent, and no retry', async () => {
		writeFileSync(join(ws, 'state', 'contacts-automation-denied'), '2026-09-24');
		const exec: string[][] = [];
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec), fetch: fakeFetch([]), isMacOS: () => true, env: { SUTANDO_ALLOW_NATIVE_PIM: '1' }, workspace: ws });
		const first = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		const second = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.equal(first.status, 'denied');
		assert.equal(second.status, 'denied');
		assert.equal(exec.length, 0);
		assert.match(String(first.instruction), /Privacy & Security/);
		assert.match(String(first.instruction), /not retry or ask again/);
	});

	it('a fresh -1743 writes the shared denial marker and the next call skips osascript', async () => {
		const exec: string[][] = [];
		const err = Object.assign(new Error('Command failed: osascript'), {
			stderr: Buffer.from('execution error: Not authorized to send Apple events to Contacts. (-1743)'),
		});
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec, err), fetch: fakeFetch([]), isMacOS: () => true, env: { SUTANDO_ALLOW_NATIVE_PIM: '1' }, workspace: ws });
		const first = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.equal(first.status, 'denied');
		assert.ok(existsSync(join(ws, 'state', 'contacts-automation-denied')), 'denial persisted for every other reader');
		const second = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.equal(second.status, 'denied');
		assert.equal(exec.length, 1, 'one attempt, never a retry');
	});

	it('an unrelated osascript failure keeps the plain error shape and writes no marker', async () => {
		const exec: string[][] = [];
		const tool = makeCallContactTool({ execFileSync: fakeExec(exec, new Error('boom (-600)')), fetch: fakeFetch([]), isMacOS: () => true, env: { SUTANDO_ALLOW_NATIVE_PIM: '1' }, workspace: ws });
		const res = (await tool.execute({ name: 'Mary' }, {} as never)) as Result;
		assert.match(String(res.error), /call_contact failed/);
		assert.ok(!existsSync(join(ws, 'state', 'contacts-automation-denied')));
	});

	it('the consent helper reads the markers the Python scripts and the briefing write', () => {
		assert.equal(checkNativePimConsent('Calendar', { env: {}, workspace: ws }).allowed, false);
		recordNativePimDenial('Calendar', ws);
		assert.ok(existsSync(join(ws, 'state', 'calendar-automation-denied')));
		const denied = checkNativePimConsent('Calendar', { env: { SUTANDO_ALLOW_NATIVE_PIM: '1' }, workspace: ws });
		assert.equal(denied.allowed, false);
		assert.equal(!denied.allowed && denied.reason, 'denied');
		assert.equal(checkNativePimConsent('Reminders', { env: { SUTANDO_ALLOW_NATIVE_PIM: '1' }, workspace: ws }).allowed, true);
	});
});
