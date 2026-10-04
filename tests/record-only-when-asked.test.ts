/**
 * record_screen_with_narration starts a recording only when her own words this turn ask for one.
 * 2026-10-04 15:43: "describe the screen by speaking and scrolling down" started a 40 s recording.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { asksToRecord, scrollAndDescribeTool } from '../src/recording-tools.js';
import { setVoiceTurnsProvider } from '../src/task-bridge.js';

test('asking to record, in either language, counts; describing while scrolling does not', () => {
	assert.equal(asksToRecord(['Can you record the screen for 30 seconds?']), true);
	assert.equal(asksToRecord(['make a demo video of this page']), true);
	assert.equal(asksToRecord(['帮我录一下屏幕']), true);
	assert.equal(asksToRecord(['Can you describe the screen by speaking and scrolling down?']), false);
	assert.equal(asksToRecord(['describe this page while scrolling down to the end']), false);
	assert.equal(asksToRecord(['边划边讲这个页面']), false);
});

test('without a recording request in her words, the tool records nothing and points to scroll', async (t) => {
	if (process.platform !== 'darwin') { t.skip('macOS-only tool'); return; }
	setVoiceTurnsProvider(() => ({
		items: [
			{ role: 'assistant', content: 'Hi.' },
			{ role: 'user', content: "Can you describe the screen by speaking and scrolling down and don't stop until you hit the end?" },
		],
	}));
	try {
		const result = await scrollAndDescribeTool.execute({}, {} as never) as Record<string, unknown>;
		assert.equal(result.status, 'not_recording');
		assert.match(String(result.instruction), /call scroll/);
	} finally {
		setVoiceTurnsProvider(null);
	}
});
