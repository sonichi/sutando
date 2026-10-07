import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { attachMeetingDictation, createMeetingEntryGate, isMeetingExitPhrase } from '../src/meeting-dictation.js';

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

describe('meeting dictation', () => {
	it('recognises exit phrases only', () => {
		for (const t of ['Sutando, come back', 'okay the meeting is over', 'end meeting', 'stop dictation', 'active mode please', 'done dictating'])
			assert.ok(isMeetingExitPhrase(t), t);
		for (const t of ['we should end the quarter strong', 'the meeting starts at noon', 'come back to this later'])
			assert.ok(!isMeetingExitPhrase(t), t);
	});

	function setup() {
		const dir = mkdtempSync(join(tmpdir(), 'meet-'));
		let mode: 'agent' | 'transcription' = 'agent';
		const buffer: string[] = [];
		const injected: string[] = [];
		let exitedByVoice = 0;
		const provider: any = { onTranscript: (t: string) => buffer.push(t) };
		const session = {
			setTranscriptionMode: async (m: 'agent' | 'transcription') => { mode = m; },
			getTranscriptionMode: () => mode,
			clearDictationBuffer: () => { buffer.length = 0; },
			injectText: async (t: string) => { injected.push(t); return true; },
		};
		const md = attachMeetingDictation({
			session, provider, notePathFor: (d) => join(dir, `notes/meeting-${d}.md`),
			onExitByVoice: () => { exitedByVoice++; }, log: () => {},
		});
		return { md, provider, buffer, injected, get mode() { return mode; }, get exitedByVoice() { return exitedByVoice; } };
	}

	it('writes each sentence to the note and exits on the phrase', async () => {
		const t = setup();
		await t.md.enter();
		assert.equal(t.mode, 'transcription');
		const path = t.md.notePath!;
		t.provider.onTranscript('first point', undefined);
		t.provider.onTranscript('second point', undefined);
		t.provider.onTranscript('Sutando, come back', undefined);
		await tick();
		assert.equal(t.mode, 'agent');
		assert.equal(t.exitedByVoice, 1);
		const note = readFileSync(path, 'utf-8');
		assert.match(note, /^---\ntitle: Meeting notes/);
		assert.match(note, /## Transcript/);
		assert.match(note, /- \[\d\d:\d\d:\d\d\] first point\n- \[\d\d:\d\d:\d\d\] second point\n$/);
		assert.doesNotMatch(note, /come back/);
		assert.equal(t.injected.length, 1);
		assert.match(t.injected[0], /2 lines/);
		assert.match(t.injected[0], /<MEETING_TRANSCRIPT_START>\nfirst point\nsecond point\n<MEETING_TRANSCRIPT_END>$/, 'the agent gets what was said');
	});

	it('keeps the words spoken before the exit phrase in the same segment', async () => {
		const t = setup();
		await t.md.enter();
		const path = t.md.notePath!;
		t.provider.onTranscript("We ship on Friday. Sutando, come back.", undefined);
		await tick();
		assert.equal(t.mode, 'agent');
		assert.match(readFileSync(path, 'utf-8'), /\] We ship on Friday\.\n$/);
	});

	it('exit() from the menu returns to agent mode without the voice callback', async () => {
		const t = setup();
		await t.md.enter();
		await t.md.exit();
		assert.equal(t.mode, 'agent');
		assert.equal(t.exitedByVoice, 0);
		await t.md.exit(); // idempotent
		assert.equal(t.injected.length, 1);
	});
});

describe('meeting entry gate', () => {
	it('enters after the confirmation turn, not the tool-call turn', () => {
		let fired = 0;
		const g = createMeetingEntryGate({ fallbackMs: 10_000, onFire: () => fired++ });
		g.schedule();
		g.noteTurnCompleted(); // turn that carried switch_mode
		assert.equal(fired, 0);
		g.noteTurnCompleted(); // "I have switched to meeting mode."
		assert.equal(fired, 1);
		g.noteTurnCompleted();
		assert.equal(fired, 1, 'fires once');
	});

	it('falls back when no confirmation turn arrives', async () => {
		let fired = 0;
		const g = createMeetingEntryGate({ fallbackMs: 10, onFire: () => fired++ });
		g.schedule();
		g.noteTurnCompleted();
		await tick(20);
		assert.equal(fired, 1);
	});

	it('cancel (switching back before entry) prevents entering', async () => {
		let fired = 0;
		const g = createMeetingEntryGate({ fallbackMs: 10, onFire: () => fired++ });
		g.schedule();
		g.cancel();
		g.noteTurnCompleted();
		g.noteTurnCompleted();
		await tick(20);
		assert.equal(fired, 0);
		assert.equal(g.pending, false);
	});
});

describe('meeting transcript carried back to the agent', () => {
	it('keeps a long meeting\'s end and says where the rest is', async () => {
		const { meetingEndedContext } = await import('../src/meeting-dictation.js');
		const long = Array.from({ length: 2000 }, (_, i) => `line ${i} ${'x'.repeat(30)}`);
		const ctx = meetingEndedContext('/n.md', long);
		assert.match(ctx, /only the end of the meeting/);
		assert.ok(ctx.includes('line 1999 '));
		assert.ok(!ctx.includes('line 0 '));
		assert.match(ctx, /<MEETING_TRANSCRIPT_START>\nline \d+ x/, 'starts on a whole line');
	});
});
