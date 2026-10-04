/**
 * On Gemini 3.8 Live, function calls are NON_BLOCKING by default, so the model narrates a
 * recording itself: it calls describe_next_screen while speaking instead of being fed by the
 * timer-driven controller. Blocking models keep the controller.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { describeNextScreenTool, modelDrivesNarration, setNarratingRecording } from '../src/recording-tools.js';

test('only 3.8 models drive the narration themselves', () => {
	assert.equal(modelDrivesNarration('gemini-3.8-live'), true);
	assert.equal(modelDrivesNarration('gemini-3.1-flash-live-preview'), false);
	assert.equal(modelDrivesNarration('gemini-2.5-flash-native-audio-preview-12-2025'), false);
	assert.equal(modelDrivesNarration(''), false);
});

test('once the recording it was narrating has ended, describe_next_screen says done and touches nothing', async () => {
	setNarratingRecording(true);
	const result = await describeNextScreenTool.execute({}, {} as never) as Record<string, unknown>;
	assert.equal(result.status, 'done');
});

test('describe_next_screen is offered for walking a page through without recording', () => {
	assert.match(describeNextScreenTool.description, /no recording and no time limit/);
	assert.doesNotMatch(describeNextScreenTool.description, /^During a narrated recording/);
});

test('on 3.8 the walk-through is declared non-blocking with a when_idle result; otherwise plain', async () => {
	const { setModelDrivesNarration } = await import('../src/recording-tools.js');
	const tool = describeNextScreenTool as typeof describeNextScreenTool & { behavior?: string };
	setModelDrivesNarration(true);
	assert.equal(tool.behavior, 'NON_BLOCKING');
	assert.equal(tool.scheduling, 'when_idle');
	setModelDrivesNarration(false);
	assert.equal(tool.behavior, undefined);
	assert.equal(tool.scheduling, undefined);
});
