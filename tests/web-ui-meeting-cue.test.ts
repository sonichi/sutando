// The meeting-mode confirmation is spoken by the page itself with the mic muted, not by the model:
// model speech can be cut off by any sound (client-VAD barge-in), and the transcriber would write it
// into the note. The server enters dictation at once and sends the fixed text.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const CLIENT = readFileSync(join(REPO, 'src', 'web-client.ts'), 'utf-8');
const AGENT = readFileSync(join(REPO, 'src', 'voice-agent.ts'), 'utf-8');

/** Pull the shipped function out of the served page rather than restating it. */
function extract(): string {
	const start = CLIENT.indexOf('function playMeetingCue(text)');
	assert.notEqual(start, -1, 'playMeetingCue() is gone or renamed');
	return CLIENT.slice(start, CLIENT.indexOf('\n}\n', start) + 2);
}

function harness(opts: { userMuted?: boolean; noSpeech?: boolean } = {}) {
	const mic: boolean[] = [];
	const spoken: Array<{ text: string; lang: string; onend?: () => void; onerror?: () => void }> = [];
	const system: string[] = [];
	const page = { muted: !!opts.userMuted };
	const voice = { setMicMuted: (m: boolean) => mic.push(m) };
	class Utterance { text: string; lang = ''; onend?: () => void; onerror?: () => void; constructor(t: string) { this.text = t; } }
	const win = opts.noSpeech ? {} : { speechSynthesis: { cancel() {}, speak: (u: Utterance) => spoken.push(u) } };
	const timers: Array<() => void> = [];
	const play = new Function(
		'window', 'SpeechSynthesisUtterance', 'voice', 'page', 'addSystem', 'setTimeout',
		`with (page) { ${extract()} return playMeetingCue; }`,
	)(win, Utterance, voice, page, (t: string) => system.push(t), (f: () => void) => timers.push(f)) as (t: string) => void;
	return { play, mic, spoken, system, page, timers };
}

const CUE = 'Meeting mode on. To bring me back, say "Sutando, come back". Until then I\'ll stay silent and take notes.';

describe('meeting-mode cue', () => {
	it('speaks the fixed sentence with the mic muted, and unmutes when it ends', () => {
		const h = harness();
		h.play(CUE);
		assert.deepEqual(h.mic, [true], 'muted before speaking');
		assert.equal(h.spoken.length, 1);
		assert.equal(h.spoken[0].text, CUE);
		assert.equal(h.spoken[0].lang, 'en-US');
		assert.deepEqual(h.system, [CUE], 'shown in the transcript too');
		h.spoken[0].onend?.();
		h.timers.forEach((f) => f());
		assert.deepEqual(h.mic, [true, false], 'unmuted once, not twice');
	});

	it('a mic the user had muted is left alone', () => {
		const h = harness({ userMuted: true });
		h.play(CUE);
		h.spoken[0].onend?.();
		assert.deepEqual(h.mic, []);
	});

	it('a user who mutes during the cue stays muted', () => {
		const h = harness();
		h.play(CUE);
		h.page.muted = true;
		h.spoken[0].onend?.();
		assert.deepEqual(h.mic, [true]);
	});

	it('a speech error, or no end event at all, still gives the mic back', () => {
		const a = harness();
		a.play(CUE);
		a.spoken[0].onerror?.();
		assert.deepEqual(a.mic, [true, false]);
		const b = harness();
		b.play(CUE);
		b.timers.forEach((f) => f());
		assert.deepEqual(b.mic, [true, false], 'the timeout releases it');
	});

	it('without speech synthesis it only shows the text and never touches the mic', () => {
		const h = harness({ noSpeech: true });
		h.play(CUE);
		assert.deepEqual(h.mic, []);
		assert.deepEqual(h.system, [CUE]);
	});

	it('the page routes a meeting.cue frame to it', () => {
		assert.match(CLIENT, /msg\.type === 'meeting\.cue'\) \{\s*playMeetingCue\(String\(msg\.text \|\| ''\)\);/);
	});
});

describe('voice agent: the model no longer speaks the confirmation', () => {
	it('every entry (switch_mode and the menu bar) sends the fixed cue, and switch_mode tells the model to say nothing', () => {
		const enter = AGENT.slice(AGENT.indexOf('function enterMeetingDictation()'), AGENT.indexOf('\n}\n', AGENT.indexOf('function enterMeetingDictation()')));
		assert.match(enter, /sendJsonToClient\(\{ type: 'meeting\.cue', text: MEETING_ENTRY_SAY \}/);
		assert.doesNotMatch(AGENT, /Say exactly this, then end your turn/);
		assert.match(AGENT, /if \(want\) enterMeetingDictation\(\);/, 'the menu-bar path enters through the same function');
		assert.doesNotMatch(AGENT, /meetingEntry\./, 'no wait for the model\'s turn before transcribing');
	});
});
