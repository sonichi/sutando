/**
 * A config the migration moved to 3.8 goes back to 3.1 once when Gemini refuses the model; a
 * bad key, a network drop, or a 3.8 the user picked is never touched. The owner is told a restart
 * is happening only where one can.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
	LEGACY_SEEDED_MODEL,
	MIGRATED_MODEL,
	MODEL_MIGRATION_KEY,
	MODEL_REVERT_KEY,
	migrateLegacyModel,
	revertModelMigration,
} from '../src/voice-config.js';
import { isModelUnavailableClose } from '../src/voice-error-classifier.js';
import { isVoiceAgentLaunchdManaged, nextSwitchConfig, restartAfterModelRevert } from '../src/voice-config-switch.js';
import type { spawnSync } from 'node:child_process';

function configWith(body: unknown): string {
	const dir = mkdtempSync(join(tmpdir(), 'voice-revert-'));
	const path = join(dir, 'voice-agent.json');
	writeFileSync(path, JSON.stringify(body, null, 2));
	return path;
}

const read = (path: string) => JSON.parse(readFileSync(path, 'utf-8'));

test('an unknown model (1008) is model-unavailable; a bad key (1007), a drop, or a stale handle is not', () => {
	assert.equal(isModelUnavailableClose(1008, 'models/gemini-9.9-live is not found for API version v1beta, or is not supported for bidiGenerateContent. Call ModelService.'), true);
	assert.equal(isModelUnavailableClose(1007, 'API key not valid. Please pass a valid API key.'), false);
	assert.equal(isModelUnavailableClose(1006, ''), false);
	assert.equal(isModelUnavailableClose(1011, 'Internal error encountered.'), false);
	assert.equal(isModelUnavailableClose(1008, 'Requested entity was not found.'), false);
	assert.equal(isModelUnavailableClose(1011, 'models/gemini-9.9-live is not found for API version v1beta'), false);
});

test('a migrated install goes back to its 3.1 model once, other keys kept, and is not migrated again', () => {
	const path = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: false, mediaResolution: 'MEDIA_RESOLUTION_LOW' });
	assert.equal(migrateLegacyModel(path).migrated, true);
	const result = revertModelMigration(path, new Date('2026-10-04T00:00:00Z'));
	assert.equal(result.reverted, true);
	assert.equal(result.model, LEGACY_SEEDED_MODEL);
	const after = read(path);
	assert.equal(after.model, LEGACY_SEEDED_MODEL);
	assert.equal(after.googleSearch, false);
	assert.equal(after.mediaResolution, 'MEDIA_RESOLUTION_LOW');
	assert.notEqual(after[MODEL_MIGRATION_KEY], undefined);
	assert.match(String(after[MODEL_REVERT_KEY]), /2026-10-04/);

	assert.equal(migrateLegacyModel(path).migrated, false);
	assert.equal(read(path).model, LEGACY_SEEDED_MODEL);
	writeFileSync(path, JSON.stringify({ ...read(path), model: MIGRATED_MODEL }));
	assert.equal(revertModelMigration(path).reverted, false);
	assert.equal(read(path).model, MIGRATED_MODEL);
});

test('the model comes from the .bak-3.1 copy, and edits made since the migration are kept', () => {
	const path = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: true });
	assert.equal(migrateLegacyModel(path).migrated, true);
	writeFileSync(`${path}.bak-3.1`, JSON.stringify({ model: 'gemini-3.1-flash-live-preview-custom', googleSearch: true }));
	writeFileSync(path, JSON.stringify({ ...read(path), googleSearch: false }));
	assert.equal(revertModelMigration(path).model, 'gemini-3.1-flash-live-preview-custom');
	assert.equal(read(path).googleSearch, false);
});

test('a user who chose 3.8 themselves, or moved off 3.8 since, is left alone', () => {
	const chosen = configWith({ model: MIGRATED_MODEL, googleSearch: false });
	assert.equal(revertModelMigration(chosen).reverted, false);
	assert.equal(read(chosen).model, MIGRATED_MODEL);

	const movedOn = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: false });
	migrateLegacyModel(movedOn);
	writeFileSync(movedOn, JSON.stringify({ ...read(movedOn), model: 'gemini-2.5-flash-native-audio-preview-12-2025' }));
	assert.equal(revertModelMigration(movedOn).reverted, false);
	assert.equal(read(movedOn).model, 'gemini-2.5-flash-native-audio-preview-12-2025');

	assert.equal(revertModelMigration(join(mkdtempSync(join(tmpdir(), 'voice-revert-')), 'none.json')).reverted, false);
});

test('a 3.8 picked with the voice switch after the migration is not reverted', () => {
	const path = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: false });
	migrateLegacyModel(path);
	writeFileSync(path, JSON.stringify(nextSwitchConfig(read(path), { model: MIGRATED_MODEL, googleSearch: true })));
	assert.equal(revertModelMigration(path).reverted, false);
	assert.equal(read(path).model, MIGRATED_MODEL);
	assert.equal(migrateLegacyModel(path).migrated, false);
});

const REVERTED = { reverted: true, model: LEGACY_SEEDED_MODEL, reason: 'migrated model unavailable' };

test('with no launchd job, nothing is restarted and the owner is told to restart Sutando', () => {
	const notices: string[] = [];
	let restarts = 0;
	const outcome = restartAfterModelRevert(REVERTED, MIGRATED_MODEL, {
		launchdManaged: false,
		restart: () => { restarts++; },
		notify: (m) => notices.push(m),
	});
	assert.equal(outcome, 'needs-manual-restart');
	assert.equal(restarts, 0);
	assert.equal(notices.length, 1);
	assert.match(notices[0], /restart Sutando to bring voice back on gemini-3\.1/);
	assert.doesNotMatch(notices[0], /is restarting/);
});

test('under launchd the restart fires; a wrapper that exits non-zero sends the restart-Sutando notice', () => {
	const notices: string[] = [];
	let onExit: ((code: number | null) => void) | undefined;
	const outcome = restartAfterModelRevert(REVERTED, MIGRATED_MODEL, {
		launchdManaged: true,
		restart: (cb) => { onExit = cb; },
		notify: (m) => notices.push(m),
	});
	assert.equal(outcome, 'restarting');
	assert.equal(notices.length, 1);
	assert.match(notices[0], /is restarting/);
	onExit!(0);
	assert.equal(notices.length, 1);
	onExit!(5);
	assert.equal(notices.length, 2);
	assert.match(notices[1], /restart Sutando/);
});

test('no revert, no restart and no notice', () => {
	const notices: string[] = [];
	const outcome = restartAfterModelRevert({ reverted: false, reason: 'already reverted once' }, MIGRATED_MODEL, {
		launchdManaged: true,
		restart: () => { throw new Error('must not restart'); },
		notify: (m) => notices.push(m),
	});
	assert.equal(outcome, 'nothing');
	assert.equal(notices.length, 0);
});

test('launchd detection reads the launchctl print exit status for the voice-agent job', () => {
	const calls: string[][] = [];
	const fake = (status: number) => ((cmd: string, args: string[]) => {
		calls.push([cmd, ...args]);
		return { status };
	}) as unknown as typeof spawnSync;
	assert.equal(isVoiceAgentLaunchdManaged(fake(0)), true);
	assert.equal(isVoiceAgentLaunchdManaged(fake(113)), false);
	assert.equal(calls[0][0], 'launchctl');
	assert.match(calls[0][2], /^gui\/\d+\/com\.sutando\.voice-agent$/);
});
