import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import type { DictationTranscriptEvent } from 'bodhi-realtime-agent';
import { attachMeetingDictation, createMeetingEntryGate, findExitCommand, isMeetingExitPhrase } from '../src/meeting-dictation.js';

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

describe('meeting dictation', () => {
	it('recognises exit phrases only', () => {
		for (const t of ['Sutando, come back', 'okay the meeting is over', 'end meeting', 'stop dictation', 'active mode please', 'done dictating',
			'Switch back to active mode.', 'Hi, Sutando come back.', 'Sutando, end the meeting.'])
			assert.ok(isMeetingExitPhrase(t), t);
		for (const t of ['we should end the quarter strong', 'the meeting starts at noon', 'come back to this later'])
			assert.ok(!isMeetingExitPhrase(t), t);
	});

	it('ignores a command phrase that is only mentioned in meeting speech', () => {
		for (const t of [
			'Do not stop dictation until we finish the budget review.',
			'How should we end the meeting?',
			'I think the meeting is over budget.',
			'Let me stop dictation for a second and explain.',
			'Next we discuss active mode in the app.',
		]) assert.ok(!isMeetingExitPhrase(t), t);
	});

	it('keeps meeting speech before an addressed command and drops fillers', () => {
		const none = { after: '', following: '' };
		assert.deepEqual(findExitCommand('We ship on Friday. Sutando, come back.'), { before: 'We ship on Friday.', ...none });
		assert.deepEqual(findExitCommand('Budget is approved. Sutando, end the meeting please.'), { before: 'Budget is approved.', ...none });
		assert.deepEqual(findExitCommand('Hi, Sutando come back.'), { before: '', ...none });
		assert.deepEqual(findExitCommand('Sutando, come back and summarize.'), { before: '', after: 'summarize.', following: '' });
		assert.deepEqual(findExitCommand('Sutando, come back, then send the notes to Chi.'), { before: '', after: 'send the notes to Chi.', following: '' });
		assert.deepEqual(findExitCommand('Sutando, come back. What did we decide?'), { before: '', after: '', following: 'What did we decide?' },
			'a further sentence stays meeting speech, not a request');
		assert.equal(findExitCommand('Stop dictation and summarize.'), null, 'an unaddressed command must stand alone');
		assert.deepEqual(findExitCommand('Okay, stop dictation.'), { before: '', ...none });
	});

	it('treats CJK text and numbers as meeting speech, never as filler', () => {
		assert.deepEqual(findExitCommand('预算已经批准。 Sutando, come back.'), { before: '预算已经批准。', after: '', following: '' });
		assert.equal(findExitCommand('我们接下来介绍 active mode.'), null);
		assert.equal(findExitCommand('500. Stop dictation.'), null);
		assert.equal(findExitCommand('好的 stop dictation'), null, 'only listed English fillers may stand next to an unaddressed command');
	});

	it('counts Sutando as addressed only at the start of a sentence', () => {
		for (const t of [
			'We said Sutando come back online and email the report to the client.',
			'Did you hear Sutando come back online yesterday?',
			'Sutando, the meeting is over budget.',
			'I asked Sutando, end the meeting notes feature is buggy.',
		]) assert.equal(findExitCommand(t), null, t);
	});

	it('stays in the meeting when a sentence merely mentions an exit phrase', async () => {
		const t = setup();
		await t.md.enter();
		t.provider.say('Do not stop dictation until we finish the budget review.');
		await tick();
		assert.equal(t.mode, 'transcription');
		assert.match(readFileSync(t.md.notePath!, 'utf-8'), /\] Do not stop dictation until we finish the budget review\.\n$/);
	});

	function setup(opts: { switching?: () => Promise<void>; notePathFor?: (d: string) => string; inject?: boolean; log?: (m: string) => void } = {}) {
		const dir = mkdtempSync(join(tmpdir(), 'meet-'));
		let mode: 'agent' | 'transcription' = 'agent';
		const buffer: string[] = [];
		const injected: string[] = [];
		let exitedByVoice = 0;
		const listeners: Array<(e: DictationTranscriptEvent) => void> = [];
		// Like bodhi: outside agent mode a final is buffered first, then sent to subscribers.
		const emit = (text: string, partial: boolean) => {
			if (mode === 'agent') return;
			if (!partial) buffer.push(text);
			for (const l of listeners) l({ text, partial });
		};
		const provider = { say: (text: string) => emit(text, false), partial: (text: string) => emit(text, true) };
		const session = {
			// Like bodhi: the mode reads as the old one until the switch (provider start/stop) finishes.
			setTranscriptionMode: async (m: 'agent' | 'transcription') => { await opts.switching?.(); mode = m; },
			getTranscriptionMode: () => mode,
			clearDictationBuffer: () => { buffer.length = 0; },
			injectText: async (t: string) => { injected.push(t); return opts.inject ?? true; },
			onDictationTranscript: (l: (e: DictationTranscriptEvent) => void) => {
				listeners.push(l);
				return () => { listeners.splice(listeners.indexOf(l), 1); };
			},
		};
		const md = attachMeetingDictation({
			session, notePathFor: opts.notePathFor ?? ((d) => join(dir, `notes/meeting-${d}.md`)),
			onExitByVoice: () => { exitedByVoice++; }, log: opts.log ?? (() => {}),
		});
		return { md, provider, buffer, injected, get mode() { return mode; }, get exitedByVoice() { return exitedByVoice; } };
	}

	it('writes each sentence to the note and exits on the phrase', async () => {
		const t = setup();
		await t.md.enter();
		assert.equal(t.mode, 'transcription');
		const path = t.md.notePath!;
		t.provider.say('first point');
		t.provider.say('second point');
		t.provider.say('Sutando, come back');
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

	it('takes finals from the dictation subscription and ignores partials', async () => {
		const t = setup();
		await t.md.enter();
		t.provider.partial('first po');
		t.provider.say('first point');
		assert.match(readFileSync(t.md.notePath!, 'utf-8'), /## Transcript[^\n]*\n- \[[\d:]+\] first point\n$/);
		assert.deepEqual(t.buffer, ['first point'], 'bodhi keeps its own buffer');
	});

	it('keeps the words spoken before the exit phrase in the same segment', async () => {
		const t = setup();
		await t.md.enter();
		const path = t.md.notePath!;
		t.provider.say("We ship on Friday. Sutando, come back.");
		await tick();
		assert.equal(t.mode, 'agent');
		assert.match(readFileSync(path, 'utf-8'), /\] We ship on Friday\.\n$/);
	});

	it('an exit requested while entry is still starting the transcriber runs after it', async () => {
		const t = setup({ switching: () => tick(20) });
		const entering = t.md.enter();
		const exiting = t.md.exit();
		await Promise.all([entering, exiting]);
		assert.equal(t.mode, 'agent', 'the session does not stay in transcription');
		assert.equal(t.md.notePath, null);
	});

	it('an entry requested while an exit is unfinished starts a new meeting after it', async () => {
		const t = setup({ switching: () => tick(20) });
		await t.md.enter();
		const exiting = t.md.exit();
		const entering = t.md.enter();
		await Promise.all([exiting, entering]);
		assert.equal(t.mode, 'transcription');
		assert.ok(t.md.notePath, 'the new meeting has its note');
		t.provider.say('second meeting point');
		assert.match(readFileSync(t.md.notePath!, 'utf-8'), /\] second meeting point\n$/);
	});

	it('a failed entry leaves no meeting behind and can be retried', async () => {
		let fail = true;
		const t = setup({ switching: async () => { if (fail) throw new Error('setup timed out'); } });
		await assert.rejects(t.md.enter(), /setup timed out/);
		assert.equal(t.mode, 'agent');
		assert.equal(t.md.notePath, null);
		fail = false;
		await t.md.enter();
		assert.equal(t.mode, 'transcription');
	});

	it('passes a request spoken with the exit command on to the agent', async () => {
		const t = setup();
		await t.md.enter();
		t.provider.say('We ship on Friday.');
		t.provider.say('Sutando, come back and summarize.');
		await tick();
		assert.equal(t.mode, 'agent');
		assert.match(t.injected[0], /the user also said: "summarize\."/);
	});

	it('keeps a sentence that only mentions Sutando coming back in the note and stays in the meeting', async () => {
		const t = setup();
		await t.md.enter();
		const line = 'We said Sutando come back online and email the report to the client.';
		t.provider.say(line);
		await tick();
		assert.equal(t.mode, 'transcription');
		assert.ok(readFileSync(t.md.notePath!, 'utf-8').endsWith(`] ${line}\n`));
		assert.deepEqual(t.injected, [], 'nothing reaches the agent as a request');
	});

	it('keeps mixed-language speech before an addressed exit in the note', async () => {
		const t = setup();
		await t.md.enter();
		const path = t.md.notePath!;
		t.provider.say('预算已经批准。 Sutando, come back.');
		await tick();
		assert.equal(t.mode, 'agent');
		assert.match(readFileSync(path, 'utf-8'), /\] 预算已经批准。\n$/);
		assert.match(t.injected[0], /预算已经批准。/);
	});

	it('a failed entry writes no transcript heading', async () => {
		const path = join(mkdtempSync(join(tmpdir(), 'meet-fail-')), 'note.md');
		const t = setup({ switching: async () => { throw new Error('setup timed out'); }, notePathFor: () => path });
		await assert.rejects(t.md.enter(), /setup timed out/);
		assert.equal(existsSync(path), false);
	});

	it('logs when the transcript cannot be delivered to the session', async () => {
		const logs: string[] = [];
		const t = setup({ inject: false, log: (m) => logs.push(m) });
		await t.md.enter();
		t.provider.say('a point');
		await t.md.exit();
		assert.ok(logs.some((m) => /transcript not delivered/.test(m)));
	});

	it('tells the agent the notes are incomplete when lines could not be written', async () => {
		const t = setup();
		await t.md.enter();
		rmSync(t.md.notePath!);
		mkdirSync(t.md.notePath!); // appending to a directory fails
		t.provider.say('lost point');
		t.provider.say('Sutando, come back');
		await tick();
		assert.match(t.injected[0], /1 of these lines could not be written to the note/);
		assert.doesNotMatch(t.injected[0], /notes are saved/);
		assert.match(t.injected[0], /lost point/, 'the agent still gets what was said');
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
