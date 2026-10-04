/**
 * On Gemini 3.8 Live, function calls are NON_BLOCKING by default, so the model narrates a
 * recording itself: it calls describe_next_screen while speaking instead of being fed by the
 * timer-driven controller. Blocking models keep the controller.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { describeNextScreenTool, modelDrivesNarration } from '../src/recording-tools.js';

test('only 3.8 models drive the narration themselves', () => {
	assert.equal(modelDrivesNarration('gemini-3.8-live'), true);
	assert.equal(modelDrivesNarration('gemini-3.1-flash-live-preview'), false);
	assert.equal(modelDrivesNarration('gemini-2.5-flash-native-audio-preview-12-2025'), false);
	assert.equal(modelDrivesNarration(''), false);
});

test('describe_next_screen outside a recording says done and touches nothing', async () => {
	const result = await describeNextScreenTool.execute({}, {} as never) as Record<string, unknown>;
	assert.equal(result.status, 'done');
});
